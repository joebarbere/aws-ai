"""Offline tests for the Polly and Transcribe wrappers."""

from __future__ import annotations

import io
from typing import Any

from aws_ai.speech import get_transcription, start_transcription, synthesize_speech


class FakePollyClient:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.last_kwargs: dict[str, Any] | None = None

    def synthesize_speech(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"AudioStream": io.BytesIO(self.audio)}


class FakeTranscribeClient:
    def __init__(self, job: dict[str, Any]) -> None:
        self.job = job
        self.last_kwargs: dict[str, Any] | None = None

    def start_transcription_job(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"TranscriptionJob": self.job}

    def get_transcription_job(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"TranscriptionJob": self.job}


def test_polly_returns_audio_bytes() -> None:
    client = FakePollyClient(b"ID3-audio-bytes")

    assert synthesize_speech(client, "hello") == b"ID3-audio-bytes"


def test_polly_voice_and_engine_are_forwarded() -> None:
    client = FakePollyClient(b"")

    synthesize_speech(client, "hi", voice_id="Matthew", engine="long-form", output_format="pcm")

    assert client.last_kwargs == {
        "Text": "hi",
        "VoiceId": "Matthew",
        "Engine": "long-form",
        "OutputFormat": "pcm",
    }


def test_transcription_starts_in_progress() -> None:
    client = FakeTranscribeClient(
        {"TranscriptionJobName": "job-1", "TranscriptionJobStatus": "IN_PROGRESS"}
    )

    job = start_transcription(client, "job-1", "s3://bucket/audio.mp3")

    assert job.status == "IN_PROGRESS"
    assert not job.is_done
    assert job.transcript_uri is None


def test_output_bucket_is_optional() -> None:
    client = FakeTranscribeClient({"TranscriptionJobName": "j", "TranscriptionJobStatus": "QUEUED"})

    start_transcription(client, "j", "s3://b/a.mp3")
    assert client.last_kwargs is not None
    assert "OutputBucketName" not in client.last_kwargs

    start_transcription(client, "j", "s3://b/a.mp3", output_bucket="results", language_code="fr-FR")
    assert client.last_kwargs["OutputBucketName"] == "results"
    assert client.last_kwargs["LanguageCode"] == "fr-FR"


def test_completed_job_exposes_transcript_uri() -> None:
    client = FakeTranscribeClient(
        {
            "TranscriptionJobName": "job-1",
            "TranscriptionJobStatus": "COMPLETED",
            "Transcript": {"TranscriptFileUri": "https://s3/transcript.json"},
        }
    )

    job = get_transcription(client, "job-1")

    assert job.is_done and job.succeeded
    assert job.transcript_uri == "https://s3/transcript.json"


def test_failed_job_is_done_but_not_successful() -> None:
    client = FakeTranscribeClient(
        {"TranscriptionJobName": "job-1", "TranscriptionJobStatus": "FAILED"}
    )

    job = get_transcription(client, "job-1")

    assert job.is_done
    assert not job.succeeded
