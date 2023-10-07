import copy
import random
import time
from typing import List
from fooocusapi.api_utils import QueueReachLimitException
from fooocusapi.models import GeneratedImage, GenerationFinishReason, PerfomanceSelection, TaskType, Text2ImgRequest
from fooocusapi.task_queue import TaskQueue
from modules.expansion import safe_str
from modules.sdxl_styles import apply_style, fooocus_expansion, aspect_ratios

task_queue = TaskQueue()

def process_generate(req: Text2ImgRequest) -> List[GeneratedImage]:
    import modules.default_pipeline as pipeline
    import modules.patch as patch
    import modules.flags as flags
    import comfy.model_management as model_management
    from modules.util import join_prompts, remove_empty_str
    from modules.private_logger import log

    task_seq = task_queue.add_task(TaskType.text2img, {
                        'body': req.__dict__})
    if task_seq is None:
        print("[Task Queue] The task queue has reached limit")
        raise QueueReachLimitException()
    
    sleep_seconds = 0
    while not task_queue.is_task_ready_to_start(task_seq):
        if sleep_seconds == 0:
            print(f"[Task Queue] Waiting for task queue become free, seq={task_seq}")
        delay = 0.1
        time.sleep(delay)
        sleep_seconds += delay
        if sleep_seconds % 10 == 0:
            print(f"[Task Queue] Already waiting for {sleep_seconds}S, seq={task_seq}")

    print(f"[Task Queue] Task queue is free, start task, seq={task_seq}")

    task_queue.start_task(task_seq)

    execution_start_time = time.perf_counter()

    loras = [(l.model_name, l.weight) for l in req.loras]
    loras_user_raw_input = copy.deepcopy(loras)

    style_selections = [s.value for s in req.style_selections]
    raw_style_selections = copy.deepcopy(style_selections)
    if fooocus_expansion in style_selections:
        use_expansion = True
        style_selections.remove(fooocus_expansion)
    else:
        use_expansion = False

    use_style = len(req. style_selections) > 0

    adaptive_cfg = 7
    patch.adaptive_cfg = adaptive_cfg
    print(f'[Parameters] Adaptive CFG = {patch.adaptive_cfg}')

    patch.sharpness = req.sharpness
    print(f'[Parameters] Sharpness = {patch.sharpness}')

    adm_scaler_positive = 1.5
    adm_scaler_negative = 0.8
    adm_scaler_end = 0.3
    patch.positive_adm_scale = adm_scaler_positive
    patch.negative_adm_scale = adm_scaler_negative
    patch.adm_scaler_end = adm_scaler_end
    print(f'[Parameters] ADM Scale = {patch.positive_adm_scale} : {patch.negative_adm_scale} : {patch.adm_scaler_end}')

    cfg_scale = req.guidance_scale
    print(f'[Parameters] CFG = {cfg_scale}')

    initial_latent = None
    denoising_strength = 1.0
    tiled = False

    if req.performance_selection == PerfomanceSelection.speed:
        steps = 30
        switch = 20
    else:
        steps = 60
        switch = 40

    pipeline.clear_all_caches()
    width, height = aspect_ratios[req.aspect_ratios_selection.value]

    sampler_name = flags.default_sampler
    scheduler_name = flags.default_scheduler
    print(f'[Parameters] Sampler = {sampler_name} - {scheduler_name}')

    raw_prompt = req.prompt
    raw_negative_prompt = req.negative_promit

    prompts = remove_empty_str([safe_str(p)
                               for p in req.prompt.split('\n')], default='')
    negative_prompts = remove_empty_str(
        [safe_str(p) for p in req.negative_promit.split('\n')], default='')

    prompt = prompts[0]
    negative_prompt = negative_prompts[0]

    extra_positive_prompts = prompts[1:] if len(prompts) > 1 else []
    extra_negative_prompts = negative_prompts[1:] if len(
        negative_prompts) > 1 else []

    seed = req.image_seed
    max_seed = int(1024 * 1024 * 1024)
    if not isinstance(seed, int):
        seed = random.randint(1, max_seed)
    if seed < 0:
        seed = - seed
    seed = seed % max_seed

    pipeline.refresh_everything(
        refiner_model_name=req.refiner_model_name,
        base_model_name=req.base_model_name,
        loras=loras
    )
    pipeline.prepare_text_encoder(async_call=False)

    positive_basic_workloads = []
    negative_basic_workloads = []

    if use_style:
        for s in style_selections:
            p, n = apply_style(s, positive=prompt)
            positive_basic_workloads.append(p)
            negative_basic_workloads.append(n)
    else:
        positive_basic_workloads.append(prompt)

    positive_basic_workloads = positive_basic_workloads + extra_positive_prompts
    negative_basic_workloads = negative_basic_workloads + extra_negative_prompts

    positive_basic_workloads = remove_empty_str(
        positive_basic_workloads, default=prompt)
    negative_basic_workloads = remove_empty_str(
        negative_basic_workloads, default=negative_prompt)

    positive_top_k = len(positive_basic_workloads)
    negative_top_k = len(negative_basic_workloads)

    tasks = [dict(
        task_seed=seed + i,
        positive=positive_basic_workloads,
        negative=negative_basic_workloads,
        expansion='',
        c=[None, None],
        uc=[None, None],
    ) for i in range(req.image_number)]

    if use_expansion:
        for i, t in enumerate(tasks):
            expansion = pipeline.expansion(prompt, t['task_seed'])
            print(f'[Prompt Expansion] New suffix: {expansion}')
            t['expansion'] = expansion
            # Deep copy.
            t['positive'] = copy.deepcopy(
                t['positive']) + [join_prompts(prompt, expansion)]

    for i, t in enumerate(tasks):
        t['c'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['positive'],
                                         pool_top_k=positive_top_k)

    for i, t in enumerate(tasks):
        t['uc'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['negative'],
                                          pool_top_k=negative_top_k)

    if pipeline.xl_refiner is not None:
        for i, t in enumerate(tasks):
            t['c'][1] = pipeline.clip_separate(t['c'][0])

        for i, t in enumerate(tasks):
            t['uc'][1] = pipeline.clip_separate(t['uc'][0])

    results: List[GeneratedImage] = []
    all_steps = steps * req.image_number

    def callback(step, x0, x, total_steps, y):
        done_steps = current_task_id * steps + step
        print(f"Finished {done_steps}/{all_steps}")

    preparation_time = time.perf_counter() - execution_start_time
    print(f'Preparation time: {preparation_time:.2f} seconds')

    process_with_error = False
    for current_task_id, task in enumerate(tasks):
        execution_start_time = time.perf_counter()

        try:
            imgs = pipeline.process_diffusion(
                positive_cond=task['c'],
                negative_cond=task['uc'],
                steps=steps,
                switch=switch,
                width=width,
                height=height,
                image_seed=task['task_seed'],
                callback=callback,
                sampler_name=sampler_name,
                scheduler_name=scheduler_name,
                latent=initial_latent,
                denoise=denoising_strength,
                tiled=tiled,
                cfg_scale=cfg_scale
            )

            for x in imgs:
                d = [
                    ('Prompt', raw_prompt),
                    ('Negative Prompt', raw_negative_prompt),
                    ('Fooocus V2 Expansion', task['expansion']),
                    ('Styles', str(raw_style_selections)),
                    ('Performance', req.performance_selection),
                    ('Resolution', str((width, height))),
                    ('Sharpness', req.sharpness),
                    ('Guidance Scale', req.guidance_scale),
                    ('ADM Guidance', str((adm_scaler_positive, adm_scaler_negative))),
                    ('Base Model', req.base_model_name),
                    ('Refiner Model', req.refiner_model_name),
                    ('Sampler', sampler_name),
                    ('Scheduler', scheduler_name),
                    ('Seed', task['task_seed'])
                ]
                for n, w in loras_user_raw_input:
                    if n != 'None':
                        d.append((f'LoRA [{n}] weight', w))
                log(x, d, single_line_number=3)

            results.append(GeneratedImage(im=imgs[0], seed=task['task_seed'], finish_reason=GenerationFinishReason.success))
        except model_management.InterruptProcessingException as e:
            print('User stopped')
            for i in range(current_task_id + 1, len(tasks)):
                results.append(GeneratedImage(im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.user_cancel))
            break
        except Exception as e:
            print('Process failed:', e)
            process_with_error = True
            results.append(GeneratedImage(im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.error))
            
        execution_time = time.perf_counter() - execution_start_time
        print(f'Generating and saving time: {execution_time:.2f} seconds')
    
    pipeline.prepare_text_encoder(async_call=True)

    print(f"[Task Queue] Finish task, seq={task_seq}")
    task_queue.finish_task(task_seq, results, process_with_error)

    return results