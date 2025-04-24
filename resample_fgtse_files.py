import os
import librosa
import soundfile as sf

# Folder with your original 16kHz .wav files
input_folder = 'fgtse_samples'
# Folder to save the new 22kHz files
output_folder = 'resampled_fgtse_samples'

os.makedirs(output_folder, exist_ok=True)

for filename in os.listdir(input_folder):
    if filename.endswith('.wav'):
        input_path = os.path.join(input_folder, filename)
        output_path = os.path.join(output_folder, filename)

        # Load audio at its original sample rate
        audio, sr = librosa.load(input_path, sr=None)

        # Only resample if not already 22050
        if sr != 22050:
            audio_22k = librosa.resample(audio, orig_sr=sr, target_sr=22050)
        else:
            audio_22k = audio

        # Save to output folder
        sf.write(output_path, audio_22k, 22050)
        print(f"Resampled {filename} to 22050 Hz")