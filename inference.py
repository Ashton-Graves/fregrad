# Copyright 2022 (c) Microsoft Corporation. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

# Copyright 2020 LMNT, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import numpy as np
import os, sys
import torch
import torchaudio
from tqdm import tqdm
from pathlib import Path

from dataset import from_path_valid as dataset_from_path_valid
from argparse import ArgumentParser

from model import FreGrad
from learner import _nested_map
from pytorch_wavelets import DWT1DInverse
from time import time
import librosa
import librosa.display
import matplotlib as mpl

mpl.rcParams.update(mpl.rcParamsDefault)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def draw_fullband_spec(audio: torch.Tensor, axs):
    audio = audio.squeeze().cpu().numpy()
    D = librosa.stft(audio)  # STFT of y
    S_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)
    librosa.display.specshow(S_db, x_axis="s", y_axis="linear", ax=axs)


def load_state_dict(model, state_dict):
    if hasattr(model, "module") and isinstance(model.module, torch.nn.Module):
        model.module.load_state_dict(state_dict["model"])
    else:
        model.load_state_dict(state_dict["model"])
    step = state_dict["step"]
    return model, step


def restore_from_checkpoint(model, model_dir, step, filename="weights"):
    try:
        checkpoint = torch.load(
            f"{model_dir}/{filename}-{step}.pt", map_location=torch.device("cpu")
        )
        model, step = load_state_dict(model, checkpoint)
        print("Loaded {}".format(f"{model_dir}/{filename}-{step}.pt"))
        return model, step
    except FileNotFoundError:
        print("Trying to load {}...".format(f"{model_dir}/{filename}.pt"))
        checkpoint = torch.load(f"{model_dir}/{filename}.pt")
        model, step = load_state_dict(model, checkpoint)
        print(
            "Loaded {} from {} step checkpoint".format(
                f"{model_dir}/{filename}.pt", step
            )
        )
        return model, step


def remove_cutoff_alias(lowpass, highpass, sr=22050):
    """* This function cut a small part of input and output signal
        * that located around cutoff frequencies
        * This help to control the in

    Args:
        lowpass (torch.Tensor): low frequency output of DWT
        highpass (torch.Tensor): high frequencey output of DWT
        sr (int, optional): sample rate of target audio. Defaults to 22050.

    Returns:
        (torch.Tensor, torch.Tensor): filtered signals
    """
    #
    lowpass = torchaudio.functional.highpass_biquad(
        lowpass, sample_rate=sr // 2, cutoff_freq=5
    )
    highpass = torchaudio.functional.highpass_biquad(
        highpass, sample_rate=sr // 2, cutoff_freq=5
    )
    lowpass = torchaudio.functional.lowpass_biquad(
        lowpass, sample_rate=sr // 2, cutoff_freq=5500
    )
    highpass = torchaudio.functional.lowpass_biquad(
        highpass, sample_rate=sr // 2, cutoff_freq=5500
    )
    return lowpass, highpass


def predict(
    model,
    spectrogram,
    target_std,
    global_cond=None,
    fast_sampling=True,
    T=None,
    alpha=None,
    alpha_cum=None,
    beta=None,
):
    with torch.no_grad():
        # Expand rank 2 tensors by adding a batch dimension.
        if len(spectrogram.shape) == 2:
            spectrogram = spectrogram.unsqueeze(0)
        spectrogram = spectrogram.to(device)

        audio = (
            torch.randn(
                spectrogram.shape[0],
                model.params.audio_channels,
                model.params.hop_samples
                * spectrogram.shape[-1]
                // model.params.audio_channels,
                device=device,
            )
            * target_std
        )

        for n in range(len(alpha) - 1, -1, -1):
            c1 = 1 / alpha[n] ** 0.5
            c2 = beta[n] / (1 - alpha_cum[n]) ** 0.5
            audio = c1 * (
                audio
                - c2
                * model(
                    audio,
                    spectrogram,
                    torch.tensor([T[n]], device=audio.device),
                    global_cond,
                ).squeeze(1)
            )

            if n > 0:
                noise = torch.randn_like(audio) * target_std
                sigma = (
                    (1.0 - alpha_cum[n - 1]) / (1.0 - alpha_cum[n]) * beta[n]
                ) ** 0.5
                audio += sigma * noise
            audio = torch.clamp(audio, -1.0, 1.0)

        return audio


def main(args):
    # * load saved params_saved.py in model_dir
    sys.path.append(os.path.join(args.model_dir))
    # * load the saved parameters of the model from "params_saved.py"
    import params_saved

    params = params_saved.params
    from params import enforce_zero_terminal_snr

    # * Override noise_schedule param for additional tests
    # ! Since our main contribution is not reducing the number of sampling steps
    # ! these predefined noise schedulers for fast sampling are purely derived from PriorGrad
    T_OVERRIDE = args.fast_iter
    if args.fast:
        if T_OVERRIDE is not None:
            if T_OVERRIDE == 6:
                NOISE_OVERRIDE = params.noise_schedule[0:1] + [
                    0.0215,
                    0.045,
                    0.08,
                    0.2,
                    0.5,
                ]  # interpolate new steps from PriorGrad since we use a transformation
            elif T_OVERRIDE == 3:
                NOISE_OVERRIDE = params.noise_schedule[0:1] + [0.1, 0.95]
            elif T_OVERRIDE == 2:
                NOISE_OVERRIDE = [0.04, 0.95]
            elif T_OVERRIDE == 12:
                NOISE_OVERRIDE = [
                    0.0001,
                    0.0005,
                    0.0008,
                    0.001,
                    0.005,
                    0.008,
                    0.01,
                    0.05,
                    0.08,
                    0.1,
                    0.2,
                    0.5,
                ]
            elif T_OVERRIDE == 50:
                NOISE_OVERRIDE = np.linspace(1e-4, 0.05, T_OVERRIDE).tolist()
            else:
                NOISE_OVERRIDE = np.linspace(1e-4, 0.05, T_OVERRIDE).tolist()
                print(
                    "WARNING: --fast_iter other than [6, 12] is given. Using linear beta schedule: performance is expected to be WORSE!"
                )
            params.inference_noise_schedule = NOISE_OVERRIDE
            print(
                "INFO: inference noise schedule updated, fast_iter {} value {}".format(
                    len(params.inference_noise_schedule),
                    params.inference_noise_schedule,
                )
            )
        else:
            T_OVERRIDE = len(params.inference_noise_schedule)

    dataset_test = dataset_from_path_valid(args.data_root, args.filelist, params)
    model = FreGrad(params)

    model, step = restore_from_checkpoint(model, args.model_dir, args.step)
    model = model.to(device)
    model.eval()

    dir_parent = Path(args.model_dir).parent
    dir_base = os.path.basename(args.model_dir)
    if args.fast:
        sample_path = os.path.join(
            dir_parent,
            # "sample_fast",
            "hyak_sample_fast", 
            dir_base + "_step{}_fast_iter{}".format(step, T_OVERRIDE),
        )
    else:
        sample_path = os.path.join(
            dir_parent, "sample_slow", dir_base + "_step{}".format(step)
        )

    os.makedirs(sample_path, exist_ok=True)

    idwt = DWT1DInverse().to(device)

    gen_dur = []
    n_samples = []

    fast_sampling = False
    training_noise_schedule = np.array(model.params.noise_schedule)
    inference_noise_schedule = (
        np.array(model.params.inference_noise_schedule)
        if fast_sampling
        else training_noise_schedule
    )

    talpha = 1 - training_noise_schedule
    talpha_cum = np.cumprod(talpha)

    beta = inference_noise_schedule
    alpha = 1 - beta
    alpha_cum = np.cumprod(alpha)

    T = []
    for s in range(len(inference_noise_schedule)):
        for t in range(len(training_noise_schedule) - 1):
            if talpha_cum[t + 1] <= alpha_cum[s] <= talpha_cum[t]:
                twiddle = (talpha_cum[t] ** 0.5 - alpha_cum[s] ** 0.5) / (
                    talpha_cum[t] ** 0.5 - talpha_cum[t + 1] ** 0.5
                )
                T.append(t + twiddle)
                break
    T = np.array(T, dtype=np.float32)

    for i, features in tqdm(enumerate(dataset_test)):
        features = _nested_map(
            features, lambda x: x.to(device) if isinstance(x, torch.Tensor) else x
        )
        with torch.no_grad():
            spectrogram = features["spectrogram"]
            target_std_lb = features["target_std"]
            target_std_hb = features["target_std_hb"]

            target_std = torch.cat(
                (target_std_lb[:, None, :], target_std_hb[:, None, :]), dim=1
            )

            if params.condition_prior:
                target_std_specdim = target_std[:, :: params.hop_samples].unsqueeze(1)
                spectrogram = torch.cat([spectrogram, target_std_specdim], dim=1)
                global_cond = None
            elif params.condition_prior_global:
                target_std_specdim = target_std[:, :: params.hop_samples].unsqueeze(1)
                global_cond = target_std_specdim
            else:
                global_cond = None

        start = time()
        with torch.no_grad():
            audio = (
                torch.randn(
                    spectrogram.shape[0],
                    model.params.audio_channels,
                    model.params.hop_samples
                    * spectrogram.shape[-1]
                    // model.params.audio_channels,
                    device=device,
                )
                * target_std
            )
            for n in range(len(alpha) - 1, -1, -1):
                c1 = 1 / alpha[n] ** 0.5
                c2 = beta[n] / (1 - alpha_cum[n]) ** 0.5
                audio = c1 * ( audio - c2 * model(
                                                audio,
                                                spectrogram,
                                                torch.tensor([T[n]], device=audio.device),
                                                global_cond,
                                            ).squeeze(1)
                )

                if n > 0:
                    noise = torch.randn_like(audio) * target_std
                    sigma = (
                        (1.0 - alpha_cum[n - 1]) / (1.0 - alpha_cum[n]) * beta[n]
                    ) ** 0.5
                    audio += sigma * noise
                audio = torch.clamp(audio, -1.0, 1.0)
        # * OPTIONAL: here, we remove the cutoff alias, a phenomenon cause by redundant information
        # * around cutoff frequency band
        if hasattr(params, "enable_remove_cutoff_alias") and params.enable_remove_cutoff_alias:
            l, h = remove_cutoff_alias(audio[:, 0:1, :], audio[:, 1:2, :])
        else:
            l, h = audio[:, 0:1, :], audio[:, 1:2, :]
        # * Convert output in wavelet domain back to origin waveform's domain
        # * Equation 7 in our paper
        audio_pred = idwt((l, [h])).squeeze(1)
        # 
        gen_dur.append(time() - start)
        n_samples.append(audio_pred.shape[-1])
        sample_name = "{:04d}.wav".format(i + 1)
        sample_name = features["filename"][0].split("/")[-1]
        torchaudio.save(
            os.path.join(sample_path, sample_name),
            audio_pred.cpu(),
            sample_rate=model.params.sample_rate,
        )
    print("RTF: ", sum(gen_dur) / sum(n_samples) * 22050)

if __name__ == "__main__":
    parser = ArgumentParser(description="runs inference from the test set filelist")
    parser.add_argument(
        "model_dir",
        help="directory containing a trained model (or full path to weights.pt file)",
    )
    parser.add_argument(
        "data_root",
        help="root of the dataset. used to save the statistics for FreGrad."
        "example: for LJSpeech, specify /path/to/your/LJSpeech-1.1",
    )
    parser.add_argument(
        "filelist",
        help="text file containing data path."
        "example: for LJSpeech, refer to ./filelists/test.txt",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=None,
        help="number of training step checkpoint to load."
        "If not provided, tries to load the symlinked weights.pt",
    )
    parser.add_argument(
        "--fast",
        "-f",
        action="store_true",
        default=False,
        help="fast sampling procedure",
    )
    parser.add_argument(
        "--fast_iter",
        "-t",
        type=int,
        default=None,
        help="number of fast inference diffusion steps for sampling."
        "6, 12, and 50 steps are officially supported. If other value is provided, linear beta schedule is used.",
    )
    main(parser.parse_args())
