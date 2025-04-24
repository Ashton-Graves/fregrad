import os
import random
import shutil

# Path to your folder
folder_path = 'LJSpeech-1.1/wavs'

# Get a list of all files (not folders) in the directory
all_files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]

# Randomly select 500 files
sample_files = random.sample(all_files, 500)

os.makedirs('sampled_files', exist_ok=True)

# Print or save the selected files
for file in sample_files:
    shutil.copy2(os.path.join(folder_path, file), os.path.join('sampled_files', file))
