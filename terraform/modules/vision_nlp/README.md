# vision_nlp

Rekognition and Comprehend — the two services on the exam with both a zero-infrastructure API and a
trainable side you can actually provision.

## Cost

| Thing | Charge |
|---|---|
| Face collection | Free to exist; ~$0.001 per image indexed or searched |
| Custom Labels project | Free to exist; training a model version ~$1/hr |
| Comprehend custom training | ~$3/hr of training time |
| Custom model at rest | **Free, as long as you never create a real-time endpoint** |

That last line is the trap. A Comprehend custom endpoint bills per inference-unit-hour whether or
not you call it, exactly like a SageMaker endpoint. This module deliberately creates no endpoint —
use `start_document_classification_job` (batch, per-use) instead.

## The distinction this module exists to show

Rekognition's `DetectLabels`, `DetectText`, and `DetectFaces` provision **nothing** — they are pure
API calls against pretrained models. A *collection* is different: it is server-side storage of face
vectors that you index into and search. Same service, two shapes, and the exam asks which is which.

Comprehend works the same way: `detect_sentiment` (in `python/src/aws_ai/comprehend.py`) needs no
infrastructure, while a custom classifier is a trained artifact.

## Both custom models need training data first

Terraform can create the training job; it cannot invent a labeled dataset. So the classifier and
recognizer are gated on you supplying S3 URIs, and a missing prefix fails minutes into training
rather than at plan time.

A minimal classifier CSV is just `label,text` with no header:

```csv
BEDROCK,which foundation models are available in this region
SAGEMAKER,deploy the model to a real time endpoint
```

Roughly 50+ examples per label is where results start being meaningful. Then:

```bash
aws s3 cp train.csv "s3://$(terraform output -raw data_bucket)/comprehend/train.csv"
terraform apply -var enable_vision_nlp=true \
  -var "classifier_training_s3_uri=s3://$(terraform output -raw data_bucket)/comprehend/train.csv"
```

## Try the collection

```bash
aws rekognition index-faces --collection-id "$(terraform output -raw rekognition_collection_id)" \
  --image-bytes fileb://face.jpg --external-image-id person-1
aws rekognition search-faces-by-image \
  --collection-id "$(terraform output -raw rekognition_collection_id)" \
  --image-bytes fileb://other.jpg
```

Note what comes back: a similarity score, not an identity. Rekognition matches vectors; the meaning
you attach to a match is your design decision, and that framing is what the responsible-AI section
of the exam is testing.
