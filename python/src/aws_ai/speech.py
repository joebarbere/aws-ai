"""Polly and Transcribe: text to speech and back.

The asymmetry is the thing to notice. Polly is synchronous — bytes come back in the response.
Transcribe is a job: you start it, poll it, and fetch the result from S3 afterwards. That
difference shows up in exam questions about which services need orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def synthesize_speech(
    client: Any,
    text: str,
    *,
    voice_id: str = "Joanna",
    engine: str = "neural",
    output_format: str = "mp3",
) -> bytes:
    """Turn text into audio and return the bytes.

    Args:
        client: A ``polly`` client.
        text: Text to speak. Pass SSML and set ``text_type`` on the client call if you need
            control over pauses and emphasis.
        voice_id: Polly voice. Neural voices sound markedly better; not every voice supports
            every engine.
        engine: ``neural``, ``standard``, or ``long-form``.
        output_format: ``mp3``, ``ogg_vorbis``, or ``pcm``.

    Returns:
        Raw audio bytes, ready to write to a file.
    """
    response = client.synthesize_speech(
        Text=text, VoiceId=voice_id, Engine=engine, OutputFormat=output_format
    )
    return bytes(response["AudioStream"].read())


@dataclass(frozen=True)
class TranscriptionJob:
    """Status of an asynchronous transcription."""

    name: str
    status: str
    transcript_uri: str | None

    @property
    def is_done(self) -> bool:
        """True once the job has stopped changing, successfully or not."""
        return self.status in {"COMPLETED", "FAILED"}

    @property
    def succeeded(self) -> bool:
        return self.status == "COMPLETED"


def start_transcription(
    client: Any,
    job_name: str,
    media_uri: str,
    *,
    language_code: str = "en-US",
    output_bucket: str | None = None,
) -> TranscriptionJob:
    """Start a transcription job.

    Args:
        client: A ``transcribe`` client.
        job_name: Unique name — reusing one in the same account raises ConflictException.
        media_uri: ``s3://`` URI of the audio.
        language_code: Source language, or use ``identify_language`` on the raw API to detect it.
        output_bucket: Where to write the transcript. Omitted means AWS holds the result in a
            service-managed bucket behind a presigned URL that expires.

    Returns:
        The job as first reported, which is normally IN_PROGRESS.
    """
    request: dict[str, Any] = {
        "TranscriptionJobName": job_name,
        "Media": {"MediaFileUri": media_uri},
        "LanguageCode": language_code,
    }

    if output_bucket is not None:
        request["OutputBucketName"] = output_bucket

    job = client.start_transcription_job(**request)["TranscriptionJob"]
    return _job_from(job)


def get_transcription(client: Any, job_name: str) -> TranscriptionJob:
    """Fetch current job status. Poll this until ``is_done``."""
    job = client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    return _job_from(job)


def _job_from(job: dict[str, Any]) -> TranscriptionJob:
    return TranscriptionJob(
        name=str(job.get("TranscriptionJobName", "")),
        status=str(job.get("TranscriptionJobStatus", "UNKNOWN")),
        transcript_uri=job.get("Transcript", {}).get("TranscriptFileUri"),
    )
