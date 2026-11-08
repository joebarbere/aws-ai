"""The bedrock-agent errors the local control plane raises.

They carry the same names and HTTP statuses as the real service's modeled exceptions, so the
runtime server (Phase 7) can return them in AWS's error shape and botocore raises the matching
``client.exceptions.*`` class.
"""

from __future__ import annotations


class BedrockAgentError(Exception):
    """Base class. ``code`` is the AWS error code; ``status`` its HTTP status."""

    code = "InternalServerException"
    status = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationException(BedrockAgentError):
    code = "ValidationException"
    status = 400


class ResourceNotFoundException(BedrockAgentError):
    code = "ResourceNotFoundException"
    status = 404


class ConflictException(BedrockAgentError):
    code = "ConflictException"
    status = 409
