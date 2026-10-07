"""CPU-only evaluation evidence. No simulator imports or implicit denominators."""

from .ledger import Ledger, LedgerError, fingerprint, load_run, summarize, compare

__all__ = ["Ledger", "LedgerError", "fingerprint", "load_run", "summarize", "compare"]
__version__ = "0.1.0"
