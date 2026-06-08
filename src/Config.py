from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Literal, ClassVar


@dataclass(frozen=True)
class General:
    INTERRUPT_COUNT: int = 8
    INTERRUT_SIZE: int = 3

    DATA_WORD_SIZE: int = 4
    CODE_WORD_SIZE: int = 5
    DATA_WORD_MASK: int = (1 << (DATA_WORD_SIZE * 8)) - 1
    CODE_WORD_MASK: int = (1 << (CODE_WORD_SIZE * 8)) - 1

    CACHE_LINE_SIZE_BYTES: int = 16
    CACHE_MISS_TICKS: int = 10
    CACHE_LINE_COUNT: int = 16
    CACHE_WAY_COUNT: int = 4


class Term(Enum):
    # Арифметические операции
    PLS   = "+"          # PLuS
    MIN   = "-"          # MINus
    DIV   = "/"          # DIVide
    MUL   = "*"          # MULtiply
    INC   = "1+"         # INCrement top of stack
    DEC   = "1-"         # DECrement top of stack

    # Операции над стеком
    SWAP  = "swap"       # SWAP
    DROP  = "drop"       # DROP
    DUP   = "dup"        # DUPlicate

    # Операции сравнения
    EQ    = "="          # EQals
    GT    = ">"          # Greater Then
    LT    = "<"          # Lower Then

    # Работа с переменными
    VST   = "!"          # Variable Save To
    VLD   = "@"          # Variable LoaD
    VCR   = "variable"   # Variablr CReate
    CCR   = "constant"   # Constant CReate
    ALLOT = "allot"      # ALLOT memory block in data section

    # Функции
    FCR   = ":"          # Function CReate

    # Ссылки
    XT    = "'"          # eXecution Token
    EXEC  = "execute"    # EXECute

    # Ввод-вывод
    PIN   = "in"         # Port INput (device id from stack)
    POUT  = "out"        # Port OUTput (device id from stack)

    # Управление потоком исполнения
    INT  = "int"        # INTerruption call
    POLY = "poly"       # POLYnomial evaluation with inline coeffs
    BYE  = "bye"        # Выход из интерпретатора


class AddressingMode(Enum):
    IMM = 0x0      # Immediate value from instruction operand
    MEM = 0x1      # Direct data-memory address from instruction operand
    STI = 0x2      # Current stack top, no pointer update: (ST)
    D0 = 0x3       # Direct register addressing
    D1 = 0x4
    D2 = 0x5
    A0 = 0x6
    A1 = 0x7
    ST_INC = 0x8   # Stack destination with pre-increment: +(ST)
    ST_DEC = 0x9   # Stack operand with post-decrement: (ST)-
    MEMI = 0xA     # Data memory addressed by current stack top: (MEM)

    # Backward-compatible spelling for old dumps/docs while the translator
    # now uses explicit D0/D1/D2/A0/A1 addressing modes.
    REG = D0


class Registers(Enum):
    D0 = 0x0
    D1 = 0x1
    D2 = 0x2
    A0 = 0x3
    A1 = 0x4


class Opcode(Enum):
    NOP   = 0x00  # No OPeration

    # Арифметические операции
    #       0x0X
    PLS   = 0x01  # PLuS
    MIN   = 0x02  # MINus
    DIV   = 0x03  # DIVide
    MUL   = 0x04  # MULtiply
    INC   = 0x05  # INCrement
    DEC   = 0x06  # DECrement

    # Операции сравнения
    #       0x2X
    EQ    = 0x20  # EQals
    GT    = 0x21  # Greater Then
    LT    = 0x22  # Lower Then

    # Операции работы с памятью
    #       0x3X
    MOV   = 0x30  # MOVe between addressing modes
    POLY  = 0x31  # POLYnomial evaluation (variable-length args)

    # IO
    #       0x4X
    IN    = 0x40  # load from port
    OUT   = 0x41  # store to port

    # Переывания
    #       0x5X
    INT   = 0x50  # call INTerruption
    IRET  = 0x51  # Interruption RETurn

    # Функции
    #       0x6X
    CALL  = 0x60  # CALL function
    RET   = 0x62  # Return

    # Управление исполнением
    #       0x7X
    JMP   = 0x70  # JuMP
    JZ    = 0x72  # Jump if Zero flag (predicate from stack)
    JNZ   = 0x73  # Jump if Not Zero flag (predicate from stack)
    HALT  = 0x71  # HALT


class Args(Enum):
    NOP   = []

    # Арифметические операции
    # Binary ops use one descriptor byte: high nibble = dst mode, low nibble = src mode.
    PLS   = [1]  # PLuS
    MIN   = [1]  # MINus
    DIV   = [1]  # DIVide
    MUL   = [1]  # MULtiply
    # INC/DEC: mode descriptor (1 byte)
    INC   = [1]  # INCrement
    DEC   = [1]  # DECrement

    # Операции сравнения
    EQ    = [1]  # EQals
    GT    = [1]  # Greater Then
    LT    = [1]  # Lower Then

    # Операции работы с памятью
    # MOV: descriptor byte (dst/src nibbles) + one 24-bit immediate/address operand.
    MOV   = [1, 3]  # MOVe
    POLY  = [1]     # POLY: degree byte + (degree+1) coeffs x 3 bytes (dynamic tail)

    # IO
    IN    = [1]  # load from port, mode selects device/result operand
    OUT   = [1]  # store to port, high nibble = device, low nibble = value

    # Переывания
    INT   = [1]  # call INTerruption
    IRET  = []   # Interruption RETurn

    # Функции
    CALL  = [1, 3]  # CALL function, mode selects direct/indirect target source
    RET   = []   # Return

    # Управление исполнением
    JMP   = [3]  # JuMP
    JZ    = [1, 3]  # Jump if Zero
    JNZ   = [1, 3]  # Jump if Not Zero
    HALT  = []   # HALT


@dataclass
class CodeElem:
    opcode: Opcode
    args: list[int]


@dataclass
class DataElem:
    value: int
    pos: int
    size: int = General.DATA_WORD_SIZE


def instruction_size(opcode: Opcode, first_word: Optional[int] = None) -> int:
    if opcode == Opcode.POLY:
        if first_word is None:
            return 2
        degree = (first_word >> (8 * (General.CODE_WORD_SIZE - 2))) & 0xFF
        return 2 + (degree + 1) * 3

    return 1 + sum(Args[opcode.name].value)


@dataclass
class LogConfig:
    """Конфигурация одного вида логирования"""
    name: str
    slice: Literal["all", "last"]
    view: str
    head: Optional[int] = None
    tail: Optional[int] = None
    assert_: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogConfig":
        return cls(
            name=data.get("name", ""),
            slice=data.get("slice", "all"),
            view=data.get("view", ""),
            head=data.get("head", None),
            tail=data.get("tail", None),
            assert_=data.get("assert", None),
        )


@dataclass
class MachineConfig:
    """Полная конфигурация машины"""
    _last_cache_enabled: ClassVar[bool] = True

    limit: int
    memory_size: int
    cache_enabled: bool
    port_mapped_io: dict[int, list[list[Any]]]
    log_configs: list[LogConfig]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MachineConfig":
        log_configs = [
            LogConfig.from_dict(item)
            for item in data.get("result", [])
        ]
        cache_enabled = cls._parse_bool(data.get("cache_enabled", data.get("cache", True)))
        cls._last_cache_enabled = cache_enabled
        return cls(
            limit=data.get("limit", 2500),
            memory_size=data.get("memory_size", 0x100),
            cache_enabled=cache_enabled,
            port_mapped_io={
                int(device_idx): entries
                for device_idx, entries in data.get("port_mapped_io", {}).items()
            },
            log_configs=log_configs,
        )

    @classmethod
    def last_cache_enabled(cls) -> bool:
        return cls._last_cache_enabled

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)
