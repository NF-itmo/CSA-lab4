from dataclasses import dataclass
from enum import Enum


INTERRUPT_COUNT = 8


class Term(Enum):
    # Арифметические операции
    PLS = "+"  # PLuS
    MINUS = "-"  # MINUS
    DIV = "/"  # DIVide
    MUL = "*"  # MULtiply
    INC = "1+"  # INCrement top of stack
    DEC = "1-"  # DECrement top of stack

    # Операции над стеком
    SWAP = "swap"  # SWAP
    DROP = "drop"  # DROP
    DUP = "dup"  # DUPlicate

    # Операции сравнения
    EQ = "="  # EQals
    GT = ">"  # Greater Then
    LT = "<"  # Lower Then

    # Работа с переменными
    VST = "!"  # Variable Save To
    VLD = "@"  # Variable LoaD
    VCR = "variable"  # Variablr CReate
    CCR = "constant"  # Constant CReate
    ALLOT = "allot"  # ALLOT memory block in data section

    # Функции
    FCR = ":"  # Function CReate

    # Ссылки
    XT = "'"  # eXecution Token
    EXEC = "execute"  # EXECute

    # Ввод-вывод
    PIN = "in"  # Port INput (device id from stack)
    POUT = "out"  # Port OUTput (device id from stack)

    # Управление потоком исполнения
    INT = "int"  # INTerruption call
    BYE = "bye"  # Выход из интерпретатора


class AddressingMode(Enum):
    IMM = 0x0  # Immediate value
    MEM = 0x1  # Data memory address
    STI = 0x2  # STack Indirect
    REG = 0x3  # REGister


class Registers(Enum):
    D0 = 0x0
    D1 = 0x1
    D2 = 0x2
    A0 = 0x3
    A1 = 0x4


class Opcode(Enum):
    NOP = 0x00  # No OPeration

    # Арифметические операции
    #       0x0X
    SPLS = 0x01  # Stack PLuS
    SMIN = 0x02  # Stack MINus
    SDIV = 0x03  # Stack DIVide
    SMUL = 0x04  # Stack MULtiply
    INC = 0x05  # INCrement
    DEC = 0x06  # DECrement

    # Операции над стеком
    #       0x1X
    SWAP = 0x10  # SWAP
    DROP = 0x11  # DROP
    DUP = 0x12  # DUPlicate
    PUSH = 0x13  # PUSH to stack
    POP = 0x14  # POP from stack

    # Операции сравнения
    #       0x2X
    EQ = 0x20  # EQals
    GT = 0x21  # Greater Then
    LT = 0x22  # Lower Then

    # Операции работы с памятью
    #       0x3X
    LD = 0x30

    # IO
    #       0x4X
    IN = 0x40  # load from port
    OUT = 0x41  # store to port

    # Переывания
    #       0x5X
    INT = 0x50  # call INTerruption
    IRET = 0x51  # Interruption RETurn

    # Функции
    #       0x6X
    CALL = 0x60  # CALL function
    SCALL = 0x61  # Stack CALL
    RET = 0x62  # Return

    # Управление исполнением
    #       0x7X
    JMP = 0x70  # JuMP
    JZ = 0x72  # Jump if Zero flag (predicate from stack)
    JNZ = 0x73  # Jump if Not Zero flag (predicate from stack)
    HALT = 0x71  # HALT


class Args(Enum):
    NOP = []

    # Арифметические операции
    SPLS = []  # Stack PLuS
    SMIN = []  # Stack MINus
    SDIV = []  # Stack DIVide
    SMUL = []  # Stack MULtiply
    # INC/DEC: mode (1 byte) + source (3 bytes)
    INC = [1, 3]  # INCrement
    DEC = []  # DECrement

    # Операции над стеком
    SWAP = []  # SWAP
    DROP = []  # DROP
    DUP = []  # DUPlicate
    # PUSH/POP: mode (1 byte) + operand (3 bytes)
    PUSH = [1, 3]  # PUSH to stack
    POP = [1, 3]  # POP from stack

    # Операции сравнения
    EQ = []  # EQals
    GT = []  # Greater Then
    LT = []  # Lower Then

    # Операции работы с памятью
    LD = [1, 3]  # LoaD register with immediate

    # IO
    IN = []  # load from port
    OUT = []  # store to port

    # Переывания
    INT = []  # call INTerruption
    IRET = []  # Interruption RETurn

    # Функции
    CALL = [3]  # CALL function
    SCALL = []  # Stack CALL
    RET = []  # Return

    # Управление исполнением
    JMP = [3]  # JuMP
    JZ = [3]  # Jump if Zero
    JNZ = [3]  # Jump if Not Zero
    HALT = []  # HALT


@dataclass
class CodeElem:
    opcode: Opcode
    args: list[int]


@dataclass
class DataElem:
    value: int
    pos: int


def instruction_size(opcode: Opcode) -> int:
    return 1 + sum(Args[opcode.name].value)
