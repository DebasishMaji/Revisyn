class RevisynError(Exception):
    """Base class for memory-store contract failures."""


class InvalidContent(RevisynError):
    pass


class InvalidSupport(RevisynError):
    pass


class NamespaceMismatch(RevisynError):
    pass


class MemoryNotFound(RevisynError):
    pass


class KeyConflict(RevisynError):
    pass


class RevisionConflict(RevisynError):
    pass


class UnsupportedMemory(RevisynError):
    pass


class SchemaError(RevisynError):
    pass


class StoreClosed(RevisynError):
    pass


class ReadOnlyStore(RevisynError):
    """A write was attempted through an operator's read-only store."""


class StoreUsageError(RevisynError):
    """A connection was used outside its owning process or thread."""


class SnapshotUsageError(RevisynError):
    """Historical inspection state cannot authorize current memory reuse or writes."""
