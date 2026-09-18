# conversational

A Lex V2 bot with one worked intent, plus the IAM role Lex needs. Transcribe and Polly appear here
only in the permissions — they provision nothing.

## Cost

The cheap end of the repo, and safe to leave applied:

| Service | Charge |
|---|---|
| Lex | ~$0.00075 per text request, ~$0.004 per speech request |
| Transcribe | ~$0.024 per minute of audio |
| Polly | Per character, with a substantial 12-month free tier |

## The three services are a pipeline

Speech in, speech out, with the reasoning in the middle:

```
audio -> Transcribe -> text -> Lex (intent + slots) -> response text -> Polly -> audio
```

Lex calls Polly itself for voice responses, which is why `polly:SynthesizeSpeech` is in the role
rather than something you invoke separately.

## After applying

The bot is created in `DRAFT` and **must be built before it will talk to you** — building is what
trains the NLU model from your sample utterances:

```bash
aws lexv2-models build-bot-locale \
  --bot-id "$(terraform output -raw lex_bot_id)" \
  --bot-version DRAFT --locale-id en_US

aws lexv2-runtime recognize-text \
  --bot-id "$(terraform output -raw lex_bot_id)" \
  --bot-alias-id TSTALIASID --locale-id en_US \
  --session-id test-1 --text "tell me about inference"
```

`TSTALIASID` is the built-in draft alias every V2 bot gets. Publishing a numbered version and a
real alias is the production step.

## Worth trying

The intent's `sample_utterance` blocks *are* the training data. Lex generalizes from them, so three
genuinely different phrasings beat twenty near-duplicates. Try adding an utterance that overlaps
another intent and watch the confidence scores move — that is what
`n_lu_intent_confidence_threshold` arbitrates, and it is the most exam-relevant knob here.
