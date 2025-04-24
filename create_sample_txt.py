import os
import random

source_dir = 'LJSpeech-1.1/wavs'
all_files = [f for f in os.listdir(source_dir) if f.endswith('.wav')]

# Randomly select 500
sampled_files = random.sample(all_files, 100)

# Write them to a new test filelist
with open('filelists/test_subset2.txt', 'w') as out:
    for f in sampled_files:
        out.write(f'LJSpeech-1.1/wavs/{f}\n')
