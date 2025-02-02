from logging import PlaceHolder
import math
import os

import gradio as gr
import torch

from modules import images, processing, prompt_parser, scripts, shared
from modules.processing import Processed, process_images
from modules.shared import cmd_opts, opts, state


def n_evenly_spaced(a, n):
    res = [a[math.ceil(i/(n-1) * (len(a)-1))] for i in range(n)]
    return res

# build prompt with weights scaled by t
def prompt_at_t(weight_indexes, prompt_list, t):
    return " AND ".join(
        [
            ":".join((prompt_list[index], str(weight * t)))
            for index, weight in weight_indexes
        ]
    )

def insert_subject_to_prompt(prompt, subject):
    return prompt.replace('[subject]', subject)


"""
Interpolate between two (or more) prompts and create an image at each step.
"""
class Script(scripts.Script):
    def title(self):
        return "Prompt morph mine"

    def show(self, is_img2img):
        return not is_img2img

    def ui(self, is_img2img):
        i1 = gr.HTML("<p style=\"margin-bottom:0.75em\">Keyframe Format: <br>Seed | Prompt or just Prompt</p>")
        subject_list = gr.TextArea(label="Subject list", placeholder="Enter one subject per line. Blank lines will be ignored.")
        prompt_list = gr.TextArea(label="Prompt", placeholder="Enter one prompt. use [subject] as placeholder for subject")
        neg_list = gr.TextArea(label="Negative prompt", placeHolder="Enter negative prompt - used for all images")
        n_images = gr.Slider(minimum=2, maximum=256, value=25, step=1, label="Number of images between keyframes")
        save_video = gr.Checkbox(label='Save results as video', value=True)
        video_fps = gr.Number(label='Frames per second', value=5)

        return [i1, prompt_list, n_images, save_video, video_fps, subject_list, neg_list]

    def run(self, p, i1, prompt_list, n_images, save_video, video_fps, subject_list, neg_list):
        # override batch count and size
        p.batch_size = 1
        p.n_iter = 1

        subjects = []
        for line in subject_list.splitlines():
            line = line.strip()
            if line == '':
                continue
            prompt_args = line.split('|')
            if len(prompt_args) == 1:  # no args
                seed, prompt = '', prompt_args[0]
            else:
                seed, prompt = prompt_args
            subjects.append((seed.strip(), prompt.strip()))



        if len(subjects) < 2:
            msg = "prompt_morph: at least 2 subjects required"
            print(msg)
            return Processed(p, [], p.seed, info=msg)
        prompt_list = [line.strip() for line in prompt_list.splitlines()]
        neg_list = [line.strip() for line in neg_list.splitlines()]

        if len(prompt_list) > 1:
            msg = f"Keep all prompts on one line: {len(prompt_list)} lines found"
            print(msg)
            return Processed(p, [], p.seed, info=msg)
        prompt_words = prompt_list[0]

        if len(neg_list) > 1:
            msg = f"Keep all Neg prompts on one line: {len(neg_list)} lines found"
            print(msg)
            return Processed(p, [], p.seed, info=msg)
        neg_words = neg_list[0]
        p.negative_prompt = neg_words

        if prompt_words.count('[subject]') != 1:
            msg = "[subject] not found in prompt list, please put one in"
            print(msg)
            return Processed(p, [], p.seed, info=msg)

        state.job_count = 1 + (n_images - 1) * (len(subjects) - 1)

        if save_video:
            import numpy as np
            try:
                import moviepy.video.io.ImageSequenceClip as ImageSequenceClip
            except ImportError:
                msg = "moviepy python module not installed. Will not be able to generate video."
                print(msg)
                return Processed(p, [], p.seed, info=msg)

        # TODO: use a timestamp instead
        # write images to a numbered folder in morphs
        morph_path = os.path.join(p.outpath_samples, "morphs")
        os.makedirs(morph_path, exist_ok=True)
        morph_number = images.get_next_sequence_number(morph_path, "")
        morph_path = os.path.join(morph_path, f"{morph_number:05}")
        p.outpath_samples = morph_path

        all_images = []
        for n in range(1, len(subjects)):
            # parsed prompts
            start_seed, start_prompt = subjects[n-1]
            target_seed, target_prompt = subjects[n]
            res_indexes, prompt_flat_list, prompt_indexes = prompt_parser.get_multicond_prompt_list([start_prompt, target_prompt])
            prompt_weights, target_weights = res_indexes

            # fix seeds. interpret '' as use previous seed
            if start_seed != '':
                if start_seed == '-1':
                    start_seed = -1
                p.seed = start_seed
            processing.fix_seed(p)

            if target_seed == '':
                p.subseed = p.seed
            else:
                if target_seed == '-1':
                    target_seed = -1
                p.subseed = target_seed
            processing.fix_seed(p)
            p.subseed_strength = 0

            # one image for each interpolation step (including start and end)
            for i in range(n_images):
                # first image is same as last of previous morph
                if i == 0 and n > 1:
                    continue
                state.job = f"Morph {n}/{len(subjects)-1}, image {i+1}/{n_images}"

                # TODO: optimize when weight is zero
                # update prompt weights and subseed strength
                t = i / (n_images - 1)
                scaled_prompt = prompt_at_t(prompt_weights, prompt_flat_list, 1.0 - t)
                scaled_target = prompt_at_t(target_weights, prompt_flat_list, t)
                subject_prompt = f'{scaled_prompt} AND {scaled_target}'
                p.prompt = insert_subject_to_prompt(prompt_words, subject_prompt)
                if p.seed != p.subseed:
                    p.subseed_strength = t
                print(f'Prompt is: {p.prompt}')
                print(f'Negative prompt is: {p.negative_prompt}')
                processed = process_images(p)
                if not state.interrupted:
                    all_images.append(processed.images[0])

        if save_video:
            clip = ImageSequenceClip.ImageSequenceClip([np.asarray(t) for t in all_images], fps=video_fps)
            clip.write_videofile(os.path.join(morph_path, f"morph-{morph_number:05}.webm"), codec='libvpx-vp9', ffmpeg_params=['-pix_fmt', 'yuv420p', '-crf', '32', '-b:v', '0'], logger=None)

        prompt = "\n".join([f"{seed} | {prompt}" for seed, prompt in subjects])
        # TODO: instantiate new Processed instead of overwriting one from the loop
        processed.all_prompts = [prompt]
        processed.prompt = prompt
        processed.info = processed.infotext(p, 0)

        processed.images = all_images
        # limit max images shown to avoid lagging out the interface
        if len(processed.images) > 25:
            processed.images = n_evenly_spaced(processed.images, 25)

        if opts.return_grid:
            grid = images.image_grid(processed.images)
            processed.images.insert(0, grid)
            if opts.grid_save:
                images.save_image(grid, p.outpath_grids, "grid", processed.all_seeds[0], processed.prompt, opts.grid_format, info=processed.infotext(p, 0), short_filename=not opts.grid_extended_filename, p=p, grid=True)

        return processed
