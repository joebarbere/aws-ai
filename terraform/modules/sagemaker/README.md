# sagemaker

A Studio domain, a user profile, an execution role, and an optional real-time endpoint.

## Cost

| Thing | Bills |
|---|---|
| Domain | Nothing by itself |
| JupyterLab app inside it | ~$0.05/hr on ml.t3.medium, **until you shut the app down** |
| Real-time endpoint | ~$0.13/hr on ml.m5.large, from create to delete, traffic or not |
| EFS home volume | Per GB stored |

Closing the browser tab does not stop a running app. Shut it down from the Studio UI, or the domain
keeps billing quietly. The endpoint is the expensive one: about $95/month if you forget it.

## The endpoint is doubly gated

`enable_endpoint = true` is not enough — `model_image` and `model_data_url` must both be set, or
the endpoint resources stay out of the plan. An endpoint with no model to serve is not a thing
worth creating by accident.

## Networking

Defaults to the account's default VPC with `PublicInternetOnly`, which is the simple study setup.
`VpcOnly` is the production answer, and it requires interface endpoints for every service Studio
talks to — a much bigger module than this one.

## The IAM shape worth noticing

`AmazonSageMakerFullAccess` is attached deliberately: Studio needs wide access to the SageMaker API
to be usable, and narrowing it turns every notebook into an IAM debugging session. The *data*
permissions are not broad — they are scoped to this project's bucket and key by an inline policy.
That split (wide on the service, narrow on the data) is the practical pattern.
