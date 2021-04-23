# JanNet

Copyright (c) 2020-2021 The JanNet developers.

## Quickstart

First, create your VM through [google cloud shell](https://ssh.cloud.google.com/) with `ctpu up --vm-only`. This way it has all the necessary permissions to connect to your Buckets and TPUs.\
Next, install the requirements with pip on your VM using `git clone https://github.com/ClashLuke/JanNet && cd JanNet && python3 -m pip install -r requirements.txt`.\
Finally, start a TPU to kick off a training run using `python3 main.py --model configs/big_ctx.json --tpu ${YOUR_TPU_NAME}`. 

## Badges

[![DeepSource](https://deepsource.io/gh/ClashLuke/JanNet.svg/?label=active+issues&show_trend=true)](https://deepsource.io/gh/ClashLuke/JanNet/?ref=repository-badge) \
[![BCH compliance](https://bettercodehub.com/edge/badge/ClashLuke/JanNet?branch=master)](https://bettercodehub.com/)

## Acknowledgements

* [Mesh Tensorflow](https://github.com/tensorflow/mesh/) as machine learning library
* Intial code forked from [Eleuther AI's GPT-Neo](https://github.com/EleutherAI/gpt-neo)

We also want to explicitly thank 
* [tensorfork](https://www.tensorfork.com/) and [TFRC](https://www.tensorflow.org/tfrc) for providing us with the required compute (TPUs)
* [Ben Wang (kindiana)](https://github.com/kingoflolz) and [Shawn Presser](https://twitter.com/theshawwn) for their invaluable knowledge about TensorFlow, TPU, and language models 
* [Tri Songz](https://github.com/trisongz/) and [Aleph Alpha](https://aleph-alpha.de/) for financing our storage and servers
