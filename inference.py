"""Command-line inference for TBDub.

The script keeps only the public single-video inference path. It supports both
full-frame input (DWPose crop + paste-back) and already cropped 512x512 input.
"""

from __future__ import annotations

import argparse
import pickle
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
import torch
from PIL import Image

from diffsynth.core import ModelConfig
from diffsynth.pipelines.tbdub import TBDubPipeline
from preprocessing import HEIGHT, WIDTH, VideoFrameView, frames_bgr_to_pil_from_video, preprocess_video_with_dwpose
from video_utils import (
    color_correction_lab,
    get_audio_length,
    get_total_length,
    iter_composited_frames,
    make_pingpong_indices,
    save_video_with_audio,
)


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CHECKPOINT_DIR = REPO_ROOT / "checkpoints"
DEFAULT_DIT_CHECKPOINTS = [
    str(DEFAULT_CHECKPOINT_DIR / "tbdub_base.safetensors"),
    str(DEFAULT_CHECKPOINT_DIR / "tbdub_finetune.safetensors"),
]


def first_existing(*candidates: Path) -> str:
    for path in candidates:
        if path.exists():
            return str(path)
    return str(candidates[0])
CLIP_NUM_FRAMES = 77
MOTION_NUM_FRAMES = 5
MOTION_LATENT_NUM_FRAMES = 2


VRAM_CONFIG = {
    "offload_dtype": torch.bfloat16,
    "offload_device": "cuda",
    "onload_dtype": torch.bfloat16,
    "onload_device": "cuda",
    "preparing_dtype": torch.bfloat16,
    "preparing_device": "cuda",
    "computation_dtype": torch.bfloat16,
    "computation_device": "cuda",
}


CACHE_VERSION = 3


@dataclass
class PreprocessedSample:
    raw_video: VideoFrameView | list
    reference_video: VideoFrameView | list
    bboxes: list[list[int]]
    audio_path: str
    video_path: str = ""


def require_paths(paths: list[str | Path]) -> None:
    missing = [str(path) for path in paths if not Path(path).exists()]
    if missing:
        formatted = "\n  - ".join(missing)
        raise FileNotFoundError(f"Required files are missing:\n  - {formatted}")


def preprocess_inputs(video_path: str, audio_path: str, args: argparse.Namespace) -> PreprocessedSample:
    sample_name = args.sample_name or Path(video_path).stem
    raw_video, reference_video, bboxes, case_flag = preprocess_video_with_dwpose(
        video_path,
        device=args.device,
        model_dir=args.dwpose_model_dir,
    )
    print(
        f"[TBDub] preprocessing complete: frames={len(reference_video)}, "
        f"crop_mode={case_flag}, first_bbox={bboxes[0]}"
    )
    return PreprocessedSample(raw_video, reference_video, bboxes, audio_path, video_path)


def load_cropped_inputs(video_path: str, audio_path: str) -> PreprocessedSample:
    frames = VideoFrameView(video_path, resize_to=(WIDTH, HEIGHT))
    bboxes = [[0, 0, WIDTH, HEIGHT] for _ in range(len(frames))]
    print(f"[TBDub] using cropped input: {len(frames)} frames at {WIDTH}x{HEIGHT}")
    return PreprocessedSample(frames, frames, bboxes, audio_path, video_path)


def save_preprocess_preview(
    frames: list[Image.Image],
    output_path: Path,
    audio_path: str,
    start_frame: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.stem}.video-only.mp4")
    arrays = [np.asarray(frame.convert("RGB")) for frame in frames]
    imageio.mimsave(
        temp_path,
        arrays,
        fps=25,
        codec="libx264",
        macro_block_size=None,
        ffmpeg_params=["-crf", "10", "-preset", "medium", "-pix_fmt", "yuv420p"],
    )
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(temp_path),
        "-ss", str(start_frame / 25.0), "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
        "-shortest", str(output_path),
    ]
    subprocess.run(command, check=True)
    temp_path.unlink(missing_ok=True)


def validate_sample(sample: PreprocessedSample) -> None:
    if len(sample.reference_video) == 0:
        raise ValueError("The input video contains no readable frames.")
    if len(sample.raw_video) != len(sample.reference_video):
        raise ValueError("Raw and reference video lengths do not match.")
    if len(sample.bboxes) != len(sample.reference_video):
        raise ValueError("Bounding-box and reference video lengths do not match.")
    if any(len(bbox) != 4 for bbox in sample.bboxes):
        raise ValueError("Every bounding box must contain [x1, y1, x2, y2].")


def build_pipeline(args: argparse.Namespace) -> tuple[TBDubPipeline, torch.Tensor]:
    checkpoint_paths = [
        *args.dit_checkpoint,
        args.text_encoder_checkpoint,
        args.vae_checkpoint,
        args.tokenizer_path,
        args.prompt_embedding,
        args.hubert_checkpoint,
    ]
    require_paths(checkpoint_paths)

    runtime_vram_config = dict(VRAM_CONFIG)
    for key in ("offload_device", "onload_device", "preparing_device", "computation_device"):
        runtime_vram_config[key] = args.device

    pipeline = TBDubPipeline.from_pretrained(
        torch_dtype=torch.bfloat16,
        device=args.device,
        model_configs=[
            ModelConfig(path=args.dit_checkpoint, **runtime_vram_config),
            ModelConfig(path=args.text_encoder_checkpoint, **runtime_vram_config),
            ModelConfig(path=args.vae_checkpoint, **runtime_vram_config),
        ],
        tokenizer_config=ModelConfig(path=args.tokenizer_path),
        args=args,
        hubert_ckpt_path=args.hubert_checkpoint,
    )
    pipeline.hubert_processor.to(dtype=torch.float32)
    prompt_embedding = torch.load(args.prompt_embedding, map_location=args.device, weights_only=True)
    return pipeline, prompt_embedding


def run_inference(
    pipeline: TBDubPipeline,
    prompt_embedding: torch.Tensor,
    sample: PreprocessedSample,
    sample_name: str,
    args: argparse.Namespace,
) -> Path:
    validate_sample(sample)
    start_frame = args.start_frame
    if start_frame < 0 or start_frame >= len(sample.reference_video):
        raise ValueError(
            f"--start-frame must be in [0, {len(sample.reference_video) - 1}], got {start_frame}."
        )
    source_raw_video = sample.raw_video[start_frame:]
    source_reference_video = sample.reference_video[start_frame:]
    source_bboxes = sample.bboxes[start_frame:]
    source_video_length = min(
        len(source_raw_video), len(source_reference_video), len(source_bboxes)
    )
    if source_video_length == 0:
        raise ValueError("No usable video frames remain after --start-frame.")

    is_student = args.inference_mode == "student"
    first_clip_preroll = is_student and args.student_first_clip_padding
    stride = CLIP_NUM_FRAMES - MOTION_NUM_FRAMES
    total_length = get_total_length(
        sample.audio_path,
        clip_num_frames=CLIP_NUM_FRAMES,
        motion_num_frames=MOTION_NUM_FRAMES,
    )
    audio_length = get_audio_length(sample.audio_path)
    real_length = min(audio_length, total_length)
    if real_length <= 0:
        raise ValueError(f"The driving audio has no usable output frames: {audio_length}.")

    if first_clip_preroll:
        num_clips = max(1, (real_length + stride - 1) // stride)
        required_video_length = num_clips * stride
    else:
        num_clips = 1 + max(0, total_length - CLIP_NUM_FRAMES) // stride
        required_video_length = total_length

    indices = make_pingpong_indices(source_video_length, required_video_length)
    if isinstance(source_reference_video, VideoFrameView):
        reference_video = source_reference_video.reindex(indices)
    else:
        reference_video = VideoFrameView(
            sample.video_path, bboxes=source_bboxes, cropped=True, indices=indices
        ) if sample.video_path else [source_reference_video[index] for index in indices]
    if isinstance(source_raw_video, VideoFrameView):
        raw_video = source_raw_video.reindex(indices)
    else:
        raw_video = VideoFrameView(
            sample.video_path, bboxes=source_bboxes, cropped=False, indices=indices
        ) if sample.video_path else [source_raw_video[index] for index in indices]
    bboxes = [source_bboxes[index] for index in indices]
    if first_clip_preroll:
        if isinstance(reference_video, VideoFrameView):
            source_indices = (
                reference_video.indices
                if reference_video.indices is not None
                else list(range(len(reference_video)))
            )
            model_reference_video = reference_video.reindex(
                [source_indices[0]] * MOTION_NUM_FRAMES + source_indices
            )
        else:
            model_reference_video = [reference_video[0]] * MOTION_NUM_FRAMES + reference_video
    else:
        model_reference_video = reference_video

    num_steps = args.num_student_steps if is_student else args.num_inference_steps
    sigma_shift = args.sigma_shift
    if sigma_shift is None:
        sigma_shift = 1.0 if is_student else 5.0

    print(
        f"[TBDub] mode={args.inference_mode}, source_frames={source_video_length}, "
        f"output_frames={real_length}, total_length={total_length}, clips={num_clips}, "
        f"steps={num_steps}, sigma_shift={sigma_shift}, "
        f"motion_from_latents={args.motion_from_latents}"
    )

    motion_video = (
        [model_reference_video[index] for index in range(MOTION_NUM_FRAMES)]
        if first_clip_preroll
        else None
    )
    motion_latents = None
    hubert_features = None
    latent_segments = []
    clip_start = 0

    for clip_index in range(num_clips):
        audio_start = clip_start - MOTION_NUM_FRAMES if first_clip_preroll else clip_start
        clip_seed = args.seed + clip_index if is_student else args.seed
        print(
            f"[TBDub] clip {clip_index + 1}/{num_clips}, "
            f"start_frame={clip_start}, audio_start={audio_start}, seed={clip_seed}"
        )
        reference_clip = [
            model_reference_video[index]
            for index in range(clip_start, min(clip_start + CLIP_NUM_FRAMES, len(model_reference_video)))
        ]
        generated_clip, outputs = pipeline(
            ref_video=reference_clip,
            start_idx=audio_start,
            audio_npy_path=None,
            audio_wav_path=sample.audio_path,
            hubert_feat=hubert_features,
            per_chunk_audio=args.per_chunk_audio,
            prompt="",
            prompt_emb=prompt_embedding,
            motion_video=motion_video,
            motion_latents=motion_latents,
            height=HEIGHT,
            width=WIDTH,
            num_frames=CLIP_NUM_FRAMES,
            motion_latents_num_frames=MOTION_LATENT_NUM_FRAMES,
            ref_cfg_scale=args.ref_cfg_scale,
            audio_cfg_scale=args.audio_cfg_scale,
            num_inference_steps=num_steps,
            sigma_shift=sigma_shift,
            seed=clip_seed,
            use_dynamic_cfg=not is_student,
            cfg_merge=not is_student,
            replace_border_latents=True,
            replace_border_latents_width=1,
            replace_border_each_step=not is_student,
            decode_output=not args.motion_from_latents,
        )
        if hubert_features is None:
            hubert_features = outputs.get("hubert_feat")
        output_latents = outputs["latents"]

        if args.motion_from_latents:
            motion_video = None
            motion_latents = output_latents[:, :, -MOTION_LATENT_NUM_FRAMES:].clone()
        else:
            generated_clip = color_correction_lab(generated_clip, reference_clip)
            motion_video = generated_clip[-MOTION_NUM_FRAMES:]

        if clip_index == 0:
            latent_segments.append(output_latents)
        else:
            latent_segments.append(output_latents[:, :, MOTION_LATENT_NUM_FRAMES:])
        clip_start += stride

    output_latents = torch.cat(latent_segments, dim=2)
    pipeline.load_models_to_device(["vae"])
    decoded = pipeline.vae.decode(
        output_latents,
        device=pipeline.device,
        tiled=False,
        tile_size=(32, 32),
        tile_stride=(16, 16),
    )
    generated_video = pipeline.vae_output_to_video(decoded)
    if first_clip_preroll:
        generated_video = generated_video[MOTION_NUM_FRAMES:]
    generated_video = generated_video[:real_length]

    output_dir = Path(args.output_dir)
    output_name = f"{sample_name}_tbdub"
    if args.cropped_input:
        save_video_with_audio(
            (color_correction_lab([frame], [reference_video[index]])[0] for index, frame in enumerate(generated_video)),
            output_name,
            str(output_dir),
            audio_path=sample.audio_path,
        )
    else:
        save_video_with_audio(
            iter_composited_frames(
                generated_video,
                reference_video[:real_length],
                raw_video[:real_length],
                bboxes[:real_length],
                replace_border_latents_width=1,
            ),
            output_name,
            str(output_dir),
            audio_path=sample.audio_path,
        )

    output_path = output_dir / f"{output_name}.mp4"
    print(f"[TBDub] output saved to {output_path}")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a lip-synchronized video with TBDub.")
    parser.add_argument(
        "--video", "--video_path", dest="video", required=True,
        help="Path to the source video or source image.",
    )
    parser.add_argument("--audio", "--audio_path", dest="audio", help="Path to the driving audio.")
    parser.add_argument(
        "--output-dir", "--output_dir", dest="output_dir", default="results",
        help="Directory for generated videos.",
    )
    parser.add_argument(
        "--sample-name", "--sample_name", dest="sample_name",
        help="Optional output stem; defaults to the input video stem.",
    )
    parser.add_argument(
        "--start-frame", "--video_start_idx", dest="start_frame", type=int, default=0,
        help="First source-video frame to use.",
    )
    parser.add_argument("--device", default="cuda:0", help="Torch device used for inference.")

    parser.add_argument(
        "--dit-checkpoint", "--ckpt_path",
        dest="dit_checkpoint",
        nargs="+",
        default=DEFAULT_DIT_CHECKPOINTS,
        help="DiT checkpoints applied from left to right (base first, fine-tuned overlay last).",
    )
    parser.add_argument(
        "--text-encoder-checkpoint",
        default=first_existing(
            DEFAULT_CHECKPOINT_DIR / "models_t5_umt5-xxl-enc-bf16.safetensors",
            DEFAULT_CHECKPOINT_DIR / "models_t5_umt5-xxl-enc-bf16.pth",
        ),
    )
    parser.add_argument(
        "--vae-checkpoint",
        default=first_existing(
            DEFAULT_CHECKPOINT_DIR / "Wan2.2_VAE.safetensors",
            DEFAULT_CHECKPOINT_DIR / "Wan2.2_VAE.pth",
        ),
    )
    parser.add_argument("--tokenizer-path", default=str(DEFAULT_CHECKPOINT_DIR / "umt5-xxl"))
    parser.add_argument("--prompt-embedding", default=str(DEFAULT_CHECKPOINT_DIR / "null_prompt_emb.pt"))
    parser.add_argument("--hubert-checkpoint", default=str(DEFAULT_CHECKPOINT_DIR / "hubert-large-ll60k"))
    parser.add_argument("--dwpose-model-dir", default=str(REPO_ROOT / "dwpose_tools" / "models"))

    parser.add_argument("--ref-cfg-scale", "--ref_cfg_scale", dest="ref_cfg_scale", type=float, default=2.0)
    parser.add_argument("--audio-cfg-scale", "--audio_cfg_scale", dest="audio_cfg_scale", type=float, default=6.0)
    parser.add_argument(
        "--audio-feat-window-size", "--audio_feat_window_size",
        dest="audio_feat_window_size", type=int, default=0,
    )
    parser.add_argument(
        "--inference-mode", "--inference_mode",
        dest="inference_mode", choices=("teacher", "student"), default="teacher",
        help="Sampling mode. Student mode uses a distilled single-branch denoising path.",
    )
    parser.add_argument(
        "--num-inference-steps", "--num_inference_steps",
        dest="num_inference_steps", type=int, default=30,
    )
    parser.add_argument(
        "--num-student-steps", "--num_student_steps",
        dest="num_student_steps", type=int, default=2,
    )
    parser.add_argument(
        "--sigma-shift", "--sigma_shift", dest="sigma_shift", type=float,
        help="FlowMatch sigma shift; defaults to 5.0 for Teacher and 1.0 for Student.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--per-chunk-audio", "--per_chunk_audio", dest="per_chunk_audio", action="store_true")
    parser.add_argument(
        "--cropped-input", "--cropped_input", dest="cropped_input", action="store_true",
        help="Skip DWPose crop and paste-back.",
    )
    parser.add_argument(
        "--motion-from-latents", "--motion_from_latents",
        dest="motion_from_latents", action="store_true",
    )
    parser.add_argument(
        "--student-first-clip-padding", "--student_first_clip_padding",
        dest="student_first_clip_padding", action="store_true", default=True,
        help="Prepend five copies of the first frame in Student mode (default).",
    )
    parser.add_argument(
        "--no-student-first-clip-padding", "--no_student_first_clip_padding",
        dest="student_first_clip_padding", action="store_false",
        help="Disable the Student first-clip pre-roll.",
    )
    parser.add_argument("--save-comparison", action="store_true", help="Also save side-by-side debug videos.")
    parser.add_argument(
        "--preprocess-only", "--preprocess_only", dest="preprocess_only", action="store_true",
        help="Save the aligned crop without loading TBDub.",
    )
    parser.add_argument(
        "--preprocess-cache", "--preprocess_cache_path", dest="preprocess_cache",
        help="Optional pickle cache for crop frames and bounding boxes.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.num_inference_steps <= 0:
        raise ValueError("--num-inference-steps must be greater than zero.")
    if args.num_student_steps <= 0:
        raise ValueError("--num-student-steps must be greater than zero.")
    if args.sigma_shift is not None and args.sigma_shift <= 0:
        raise ValueError("--sigma-shift must be greater than zero.")

    args.video = str(Path(args.video).expanduser().resolve())
    if args.audio:
        args.audio = str(Path(args.audio).expanduser().resolve())
    require_paths([args.video])
    if not args.preprocess_only and not args.audio:
        raise ValueError("--audio is required unless --preprocess-only is used.")

    audio_path = args.audio or args.video
    require_paths([audio_path])
    sample_name = args.sample_name or Path(args.video).stem
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_path = Path(args.preprocess_cache) if args.preprocess_cache else None
    sample = None
    if cache_path and cache_path.exists() and not args.cropped_input:
        try:
            with cache_path.open("rb") as file:
                cached = pickle.load(file)
            bboxes = cached.get("bboxes")
            if cached.get("version") == CACHE_VERSION and bboxes and "raw_video" not in cached:
                raw_video = VideoFrameView(args.video, bboxes=bboxes, cropped=False, count=len(bboxes))
                reference_video = VideoFrameView(args.video, bboxes=bboxes, cropped=True, count=len(bboxes))
                sample = PreprocessedSample(raw_video, reference_video, bboxes, audio_path, args.video)
                print(f"[TBDub] loaded preprocess cache: {cache_path}")
            else:
                print(f"[TBDub] ignoring incompatible preprocess cache: {cache_path}")
                cache_path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[TBDub] failed to load preprocess cache ({exc}), recomputing")
            cache_path.unlink(missing_ok=True)
            sample = None
    if sample is None:
        sample = (
            load_cropped_inputs(args.video, audio_path)
            if args.cropped_input
            else preprocess_inputs(args.video, audio_path, args)
        )
        if cache_path and not args.cropped_input:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with cache_path.open("wb") as file:
                pickle.dump(
                    {
                        "version": CACHE_VERSION,
                        "bboxes": sample.bboxes,
                    },
                    file,
                )
            print(f"[TBDub] saved preprocess cache: {cache_path}")

    validate_sample(sample)
    if args.start_frame < 0 or args.start_frame >= len(sample.reference_video):
        raise ValueError(
            f"--start-frame must be in [0, {len(sample.reference_video) - 1}], got {args.start_frame}."
        )

    if args.preprocess_only:
        preview_path = output_dir / f"{sample_name}_crop.mp4"
        save_preprocess_preview(
            sample.reference_video[args.start_frame:],
            preview_path,
            audio_path,
            args.start_frame,
        )
        print(f"[TBDub] crop preview saved to {preview_path}")
        return

    pipeline, prompt_embedding = build_pipeline(args)
    run_inference(pipeline, prompt_embedding, sample, sample_name, args)


if __name__ == "__main__":
    main()
