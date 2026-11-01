"""Project Shepherd: single-active coordination for one Desk conversation."""

from .checkpoints import Checkpoints
from .claude_connector import ClaudeConnector
from .codex_connector import CodexFamilyConnector
from .coordinator import Conflict, Coordinator, InvalidState
from .harness import ExecutionHarness
from .ownership import Ownership
from .pi_connector import PiConnector
from .projector import ReadProjector
from .runners import ClaudeRunner, CodexFamilyRunner, PiRunner
from .source_resolver import SourceResolver
from .source_receipts import SourceReceipts
from .service import Service
from .store import PromptNotStarted, SourceVerificationError, Store
from .switch import SwitchRunner
from .turn_inputs import TurnInputs

__all__ = ["Checkpoints", "ClaudeConnector", "ClaudeRunner", "CodexFamilyConnector",
           "CodexFamilyRunner", "Conflict",
           "Coordinator", "ExecutionHarness", "InvalidState", "Ownership", "PiConnector", "PiRunner", "PromptNotStarted", "ReadProjector",
           "SourceReceipts", "SourceResolver", "SourceVerificationError", "Service", "Store", "SwitchRunner", "TurnInputs"]
