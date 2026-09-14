import math
import os
import subprocess

import cv2
import imageio.v2 as imageio
import numpy as np
from PIL import Image

from diffsynth.thirdparties.utils import load_audio

def make_pingpong_indices(n: int, target_length: int):
    """Repeat a finite source sequence forward and backward."""
    assert n >= 1, "num_frames must be >= 1"
    if target_length <= n:
        return list(range(target_length))
    if n == 1:
        return [0] * target_length
    period = 2 * (n - 1)
    indices = []
    for index in range(target_length):
        remainder = index % period
        indices.append(remainder if remainder < n else period - remainder)
    return indices


def get_total_length(audio_path: str, clip_num_frames: int = 77, motion_num_frames: int = 5):
    audio = load_audio(audio_path, sr=16000)
    audio_frame_len = int(math.ceil(len(audio) / 16000 * 25))
    if audio_frame_len <= clip_num_frames:
        return clip_num_frames
    return clip_num_frames + int(
        math.ceil((audio_frame_len - clip_num_frames) / (clip_num_frames - motion_num_frames))
    ) * (clip_num_frames - motion_num_frames)

def get_audio_length(audio_path: str):
    audio = load_audio(audio_path, sr=16000)
    audio_frame_len = int(len(audio) / 16000 * 25)
    return audio_frame_len


def color_correct_one(generated_frame, source_frame, strength=1.0):
    """Official Lab match for a single pair of frames."""
    from skimage import color as skimage_color

    if strength == 0.0:
        return generated_frame

    gen_frame = np.array(generated_frame.convert("RGB")) / 255.0
    ref_frame = np.array(source_frame.convert("RGB")) / 255.0
    try:
        ref_lab = skimage_color.rgb2lab(ref_frame)
        gen_lab = skimage_color.rgb2lab(gen_frame)
    except ValueError as exc:
        print(f"Warning: Could not convert image to Lab: {exc}. Using original frame.")
        return generated_frame

    corrected_lab = gen_lab.copy()
    for channel in range(3):
        mean_gen, std_gen = gen_lab[:, :, channel].mean(), gen_lab[:, :, channel].std()
        mean_ref, std_ref = ref_lab[:, :, channel].mean(), ref_lab[:, :, channel].std()
        if std_gen == 0:
            corrected_lab[:, :, channel] = mean_ref
        else:
            corrected_lab[:, :, channel] = (corrected_lab[:, :, channel] - mean_gen) * (std_ref / std_gen) + mean_ref
    try:
        corrected_rgb = skimage_color.lab2rgb(corrected_lab)
    except ValueError as exc:
        print(f"Warning: Could not convert frame back to RGB: {exc}. Using original frame.")
        return generated_frame
    corrected_rgb = np.clip(corrected_rgb, 0.0, 1.0)
    blended_rgb = np.clip((1 - strength) * gen_frame + strength * corrected_rgb, 0.0, 1.0)
    return Image.fromarray((blended_rgb * 255).astype(np.uint8))


def color_correction_lab(generated_video, source_video, strength=1.0):
    """Match generated-frame Lab statistics to the corresponding source frame."""
    if strength == 0.0:
        return generated_video
    count = min(len(generated_video), len(source_video))
    return [color_correct_one(generated_video[i], source_video[i], strength) for i in range(count)]


def concat_pil_videos_horizontally(*videos):
    lengths = [len(video) for video in videos]
    assert len(set(lengths)) == 1, f"Length mismatch: {lengths}"
    merged_video = []
    for frames in zip(*videos):
        frames = [frame.convert("RGB") for frame in frames]
        width = sum(frame.width for frame in frames)
        height = max(frame.height for frame in frames)
        merged_frame = Image.new("RGB", (width, height))
        offset_x = 0
        for frame in frames:
            merged_frame.paste(frame, (offset_x, 0))
            offset_x += frame.width
        merged_video.append(merged_frame)
    return merged_video


def normalize_blend_edge(edge, height, width):
    max_edge = max(1, min(height, width) // 2 - 1)
    edge = max(1, min(edge, max_edge))
    if edge % 2 == 0:
        edge = edge + 1 if edge < max_edge else edge - 1
    return max(1, edge)


def build_blurred_border_mask(height, width, edge_up, edge_down, edge_left, edge_right, blur_kernel_size):
    blur_kernel_size = normalize_blend_edge(blur_kernel_size, height, width)
    mask = np.ones([height, width], dtype=np.float32)
    mask[edge_up:height - edge_down, edge_left:width - edge_right] = 0
    mask = cv2.blur(mask, (blur_kernel_size, blur_kernel_size))
    mask = cv2.blur(mask, (blur_kernel_size, blur_kernel_size))
    return mask[:, :, np.newaxis]


def apply_edge_mask(
    img_generated,
    img_reference,
    edge_margin,
    blur_kernel_size,
    is_up_all=False,
    is_below_all=False,
    is_left_all=False,
    is_right_all=False,
):
    height, width = img_generated.shape[:2]
    edge_margin = max(0, min(edge_margin, min(height, width) // 2 - 1))
    edge_up = 0 if is_up_all else edge_margin
    edge_down = 0 if is_below_all else edge_margin
    edge_left = 0 if is_left_all else edge_margin
    edge_right = 0 if is_right_all else edge_margin

    mask = build_blurred_border_mask(
        height,
        width,
        edge_up=edge_up,
        edge_down=edge_down,
        edge_left=edge_left,
        edge_right=edge_right,
        blur_kernel_size=blur_kernel_size,
    )
    mask = np.concatenate((mask, mask, mask), axis=2)

    blended = img_generated * (1 - mask) + img_reference * mask
    blended[blended > 255] = 255
    blended[blended < 0] = 0
    return blended.astype(np.uint8)


def apply_inner_edge_mask(img_generated, img_reference, pixel_margin):
    height, width = img_generated.shape[:2]
    mask = build_blurred_border_mask(
        height,
        width,
        edge_up=pixel_margin,
        edge_down=pixel_margin,
        edge_left=pixel_margin,
        edge_right=pixel_margin,
        blur_kernel_size=33,
    )
    mask = np.concatenate((mask, mask, mask), axis=2)

    blended = img_generated * (1 - mask) + img_reference * mask
    blended[blended > 255] = 255
    blended[blended < 0] = 0
    return blended.astype(np.uint8)


def blend_one_with_ref(generated_frame, ref_frame, replace_border_latents_width):
    pixel_margin = replace_border_latents_width * 16
    generated = np.array(generated_frame.convert("RGB"), dtype=np.float32)
    reference = np.array(ref_frame.convert("RGB"), dtype=np.float32)
    return Image.fromarray(apply_inner_edge_mask(generated, reference, pixel_margin))


def paste_one_back(generated_frame, raw_frame, bbox):
    generated = np.array(generated_frame.convert("RGB"), dtype=np.uint8)
    raw = np.array(raw_frame.convert("RGB"), dtype=np.uint8).copy()
    x1, y1, x2, y2 = bbox
    resized = cv2.resize(generated, (x2 - x1, y2 - y1))
    original_region = raw[y1:y2, x1:x2].copy()
    blended_region = apply_edge_mask(
        resized.astype(np.float32),
        original_region.astype(np.float32),
        edge_margin=8,
        blur_kernel_size=17,
        is_up_all=(y1 == 0),
        is_below_all=(y2 >= raw.shape[0]),
        is_left_all=(x1 == 0),
        is_right_all=(x2 >= raw.shape[1]),
    )
    raw[y1:y2, x1:x2] = blended_region
    return Image.fromarray(raw)


def blend_crop_video_with_ref(video, ref_video_frames, replace_border_latents_width):
    num_frames = min(len(video), len(ref_video_frames))
    return [
        blend_one_with_ref(video[frame_idx], ref_video_frames[frame_idx], replace_border_latents_width)
        for frame_idx in range(num_frames)
    ]


def paste_video_back(video, raw_video_frames, bboxes):
    num_frames = min(len(video), len(raw_video_frames), len(bboxes))
    return [
        paste_one_back(video[frame_idx], raw_video_frames[frame_idx], bboxes[frame_idx])
        for frame_idx in range(num_frames)
    ]


def iter_composited_frames(generated_video, reference_video, raw_video, bboxes, replace_border_latents_width=1):
    """Yield pasted full frames one by one and release generated frames."""
    count = min(len(generated_video), len(reference_video), len(raw_video), len(bboxes))
    for index in range(count):
        generated = generated_video[index]
        reference = reference_video[index]
        corrected = color_correct_one(generated, reference)
        blended = blend_one_with_ref(corrected, reference, replace_border_latents_width)
        pasted = paste_one_back(blended, raw_video[index], bboxes[index])
        if isinstance(generated_video, list):
            generated_video[index] = None
        yield pasted


def _as_rgb_array(frame):
    array = np.array(frame.convert("RGB"), dtype=np.uint8)
    return array


def save_video_with_audio(output_video, sample_name, output_dir, audio_path=None, audio_start_idx=None):
    print(f"[save_video_with_audio] Saving to {output_dir}/{sample_name}.mp4")
    os.makedirs(output_dir, exist_ok=True)
    temp_video_path = f"{output_dir}/{sample_name}_tmp.mp4"
    final_video_path = f"{output_dir}/{sample_name}.mp4"

    stream = iter(output_video)
    try:
        first = next(stream)
    except StopIteration as exc:
        raise ValueError("No video frames to save.") from exc
    first = _as_rgb_array(first)
    height, width = first.shape[:2]
    target_height = height + (height % 2)
    target_width = width + (width % 2)

    def padded(frame):
        array = _as_rgb_array(frame) if not isinstance(frame, np.ndarray) else frame
        if array.shape[0] != target_height or array.shape[1] != target_width:
            array = np.pad(
                array,
                ((0, target_height - array.shape[0]), (0, target_width - array.shape[1]), (0, 0)),
                mode="edge",
            )
        return array

    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{target_width}x{target_height}", "-r", "25", "-i", "-",
        "-c:v", "libx264", "-crf", "10", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        temp_video_path,
    ]
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert encoder.stdin is not None
    encoder.stdin.write(padded(first).tobytes())
    del first
    for frame in stream:
        encoder.stdin.write(padded(frame).tobytes())
    encoder.stdin.close()
    if encoder.wait() != 0:
        raise RuntimeError("ffmpeg failed to encode the output video.")

    assert audio_path is not None and os.path.exists(audio_path), f"audio path not found: {audio_path}"

    if audio_start_idx is not None:
        audio_start_time = audio_start_idx / 25.0
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-i",
            temp_video_path,
            "-ss",
            str(audio_start_time),
            "-i",
            audio_path,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            final_video_path,
        ]
    else:
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-i",
            temp_video_path,
            "-i",
            audio_path,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            final_video_path,
        ]

    subprocess.run(ffmpeg_cmd, check=True)
    if os.path.exists(temp_video_path):
        os.remove(temp_video_path)
