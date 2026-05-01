from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolCall:
    tool_use_id: str
    server: str
    method: str
    model: str
    session_id: str
    sequence_id: int
    position_in_seq: int
    output_tokens: int
    result_chars: int      # -1 if no matching tool_result found
    is_error: bool
    timestamp: float
    call_file: str
    call_line: int
    result_line: int       # -1 if no matching tool_result found


@dataclass(slots=True)
class Sequence:
    sequence_id: int
    session_id: str
    start_ts: float
    call_count: int = 0


@dataclass
class Store:
    calls: list[ToolCall] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    seen_uuids: set[str] = field(default_factory=set)
    durations: list[float] = field(default_factory=list)
