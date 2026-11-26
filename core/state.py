from enum import StrEnum

class ChallengeStatus(StrEnum):
    PENDING='pending'
    ASSIGNED='assigned'
    RUNNING='running'
    VERIFYING='verifying'
    SUBMITTING='submitting'
    PAUSED='paused'
    NEEDS_MANUAL_INTERVENTION='needs_manual_intervention'
    FAILED='failed'
    COMPLETED='completed'
    UNAVAILABLE='unavailable'

class SubmissionStatus(StrEnum):
    NOT_SUBMITTED='not_submitted'
    SUBMITTING='submitting'
    PENDING_CONFIRMATION='pending_confirmation'
    CONFIRMED='confirmed'
