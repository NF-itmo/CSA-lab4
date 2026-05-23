from dataclasses import dataclass, field
import re
from Config import (
    DataElem,
    CodeElem,
    Opcode,
    Term,
    AddressingMode,
    Registers,
    Args,
    instruction_size,
    INTERRUPT_COUNT,
)
from typing import Optional, Self


def to_bytes(value: int, size: int) -> bytes:
    return value.to_bytes(size, byteorder="big")


class AutoPtr[T]:
    """
    Коллекция с автоматическим счётчиком
    """

    def __init__(self, collection: list[T]) -> None:
        """Инициализация коллекции

        Args:
            collection (list[T]): коллекция
        """
        self._collection = collection
        self._ptr: int = 0

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def collection(self) -> list[T]:
        return self._collection

    def __iadd__(self, item: T) -> Self:
        self._collection.append(item)
        self._ptr += 1
        return self

    def __add__(self, other: "AutoPtr[T]") -> None:
        self._collection.extend(other.collection)
        self._ptr += other.ptr

    def alloc(self, size: int) -> None:
        self._ptr += size


@dataclass
class DataMemory:
    """
    Секция с данными
    """

    Section: AutoPtr[DataElem] = field(default_factory=lambda: AutoPtr([]))

    @property
    def ptr(self) -> int:
        return self.Section.ptr

    @property
    def collection(self) -> list[DataElem]:
        return self.Section.collection

    def alloc(
        self, values: Optional[list[int]] = None, size: Optional[int] = None
    ) -> int:
        if values is None and size is None:
            raise ValueError("Size or values array must be defined!")
        if size is not None and size < 0:
            raise ValueError(f"Block size must be non-negative, got: {size}")

        size = len(values) if size is None else size  # pyright: ignore[reportArgumentType]

        start_addr = self.Section.ptr
        current_addr = start_addr

        # Забиваем нулями, если нет предустановленного значения
        if values is None:
            values = [0 for _ in range(size)]

        # Забиваем в память
        for value in values:
            self.Section += DataElem(value, current_addr)

        return start_addr


@dataclass
class CodeMemory:
    """
    Память с кодом

    Секции:
    + JumpSection - сюда подставляется jump на старт программы
    + VectorSection - сюда пишем адреа обработчиков, которые будут вызваны при прерывании
    + ProgramSection - секция с кодом проги
    + HandlerSection - секция с обработчиками прерываний
    """

    JumpSection: AutoPtr[CodeElem] = field(default_factory=lambda: AutoPtr([]))
    VectorSection: AutoPtr[CodeElem] = field(default_factory=lambda: AutoPtr([]))
    ProgramSection: AutoPtr[CodeElem] = field(default_factory=lambda: AutoPtr([]))
    HandlerSection: AutoPtr[CodeElem] = field(default_factory=lambda: AutoPtr([]))

    @property
    def ptr(self) -> int:
        """
        Высчитывает позицию последнего слова

        Returns:
            int: индекс позиции
        """
        return (
            self.JumpSection.ptr
            + self.VectorSection.ptr
            + self.ProgramSection.ptr
            + self.HandlerSection.ptr
        )

    @property
    def collection(self) -> list[CodeElem]:
        """
        Собирает все подсекции в одину

        Returns:
            list[CodeElem]: список машинных слов
        """
        return (
            self.JumpSection.collection
            + self.VectorSection.collection
            + self.ProgramSection.collection
            + self.HandlerSection.collection
        )

    @property
    def vector_table_size(self) -> int:
        """
        Получение размера секции векторов прерывания в байтах

        Считаем как [число векторов] * 4

        Returns:
            int: размер секции
        """
        return INTERRUPT_COUNT * 4

    def _section_size_bytes(self, section: AutoPtr[CodeElem]) -> int:
        """
        Считаем размер секции в байтах
        """
        return sum(instruction_size(instr.opcode) for instr in section.collection)

    @property
    def entry_prefix_size(self) -> int:
        """
        Адрес начала программы в байтах

        Returns:
            int: адрес начала программы в байтах
        """
        return (
            instruction_size(Opcode.JMP)
            + instruction_size(Opcode.LD) * 2
            + self.vector_table_size
        )

    @property
    def program_ptr_byte_addr(self) -> int:
        """
        Текущий адрес в байтах

        Returns:
            int: тукцщий адрес программы
        """
        return self.entry_prefix_size + self._section_size_bytes(self.ProgramSection)

    def program_start(self) -> int:
        """
        Считает старт начала программы (в инструкциях)

        Returns:
            int: порядковый индекс старта программы
        """
        return self.JumpSection.ptr + self.VectorSection.ptr

    def add_to_program(self, instruction: CodeElem) -> int:
        """
        Добавить инструкцию в секцию программы

        Args:
            instruction (CodeElem): информация об инструкции

        Returns:
            int: порядковый индекс добавленной инструкции с начала программы
        """
        addr = self.program_start() + self.ProgramSection.ptr
        self.ProgramSection += instruction

        return addr

    def add_to_handler(self, instruction: CodeElem) -> int:
        addr = self.program_start() + self.ProgramSection.ptr + self.HandlerSection.ptr
        self.HandlerSection += instruction
        return addr


class Translator:
    """
    Транслятор кода в машинный код
    """

    def __init__(self, program: str) -> None:
        self._tokens = self._split(program)
        self._cursor = 0

        self._data = DataMemory()
        self._code = CodeMemory()

        self._vars: dict[
            str, int
        ] = {}  # мапа название переменной - адрес переменной (data mem)
        self._const: dict[
            str, int
        ] = {}  # мапа название константы - адрес литерала (data mem)
        self._func: dict[
            str, int
        ] = {}  # мапа название функции - адрес старта (code mem)
        self._interrupt_vectors: dict[
            int, int
        ] = {}  # мапа номер вектора - адрес хэндлера (code mem)
        self._literal_ptrs: dict[
            int, int
        ] = {}  # мапа значение литерала - адрес (data mem)
        self._string_ptrs: dict[
            str, int
        ] = {}  # мапа строка -> адрес c-string в data mem

    @staticmethod
    def _split(program: str) -> list[str]:
        """
            Разбиение программы на слова

            Args:
                program (str): Текст программы

        Returns:
            list[str]: Список слов
        """
        tokens: list[str] = []
        idx = 0
        length = len(program)

        while idx < length:
            char = program[idx]
            if char.isspace():
                idx += 1
                continue

            if char == '"':
                idx += 1
                buf: list[str] = []
                while idx < length:
                    cur = program[idx]

                    if cur == '"':
                        idx += 1
                        break

                    buf.append(cur)
                    idx += 1
                else:
                    raise ValueError("Unterminated string literal")

                tokens.append(f"\"{''.join(buf)}\"")
                continue

            start = idx
            while idx < length and not program[idx].isspace():
                idx += 1
            tokens.append(program[start:idx])

        return tokens

    @staticmethod
    def _parse_int(token: str) -> Optional[int]:
        """
        Безопасный парсинг чисел

        Args:
            token (str): токен

        Returns:
            Optinal[int] - None если токен невалиден
        """
        try:
            return int(token)
        except ValueError:
            return None

    def _has_tokens(self) -> bool:
        """
        Есть ли ещё токены

        Returns:
            bool: очев? очев!
        """
        return self._cursor < len(self._tokens)

    def _peek_token(self) -> Optional[str]:
        """
        Небезопасное получение токенов

        Returns:
            Optional[str]: если возможно, то выдаёт токен, иначе - None
        """
        if not self._has_tokens():
            return None
        return self._tokens[self._cursor]

    def _next_token(self, err_msg: str = "Syntax error") -> str:
        """
        Безопасное получение токенов

        Raises:
            ValueError: если программа кончилась

        Returns:
            str: следующий токен
        """
        if not self._has_tokens():
            raise ValueError(err_msg)

        token = self._tokens[self._cursor]
        self._cursor += 1
        return token

    def _emit(self, opcode: Opcode, args: Optional[list[int]] = None) -> int:
        """
        Обёртка для добавления инструкций в память команд

        Args:
            opcode (Opcode): Опкод инструкции
            args (Optional[list[int]]): Список аргументов инструкции

        Returns:
            int: порядковый индекс добавленной инструкции с начала программы
        """
        return self._code.add_to_program(CodeElem(opcode, args or []))

    def _literal_addr(self, value: int) -> int:
        """
        Получение адреса литерала или его создание (при отсутсвии)

        Args:
            value (int): значение литерала

        Returns:
            int: адрес литерала в памяти данных
        """
        addr = self._literal_ptrs.get(value)
        if addr is not None:
            return addr

        addr = self._data.alloc([value])
        self._literal_ptrs[value] = addr
        return addr

    def _cstring_addr(self, value: str) -> int:
        """
        Создание строки

        Args:
            value (str): содержимое строки

        Returns:
            int: адрес начала строки
        """
        # addr = self._string_ptrs.get(value)
        # if addr is not None:
        #     return addr

        addr = self._data.alloc([ord(char) for char in value] + [0])
        self._string_ptrs[value] = addr
        return addr

    @staticmethod
    def _to_term(token: str) -> Term:
        """
        Безопасное преобразование токена в терм

        Args:
            token (str): токен

        Returns:
            Term: очев

        Raises:
            ValueError: если такого токена не существует
        """
        try:
            return Term(token)
        except ValueError:
            raise ValueError(f"Unknown token: {token}")

    def _parse_until(self, stop_tokens: set[str] | None = None) -> Optional[str]:
        while self._has_tokens():
            token = self._next_token("Unexpected end of program")
            if stop_tokens is not None and token in stop_tokens:
                return token

            self._parse_token(token)

        if stop_tokens is not None:
            expected = ", ".join(sorted(stop_tokens))
            raise ValueError(f"Expected one of ({expected}) before end of program")
        return None

    def _patch_program_jump(self, program_index: int, target_addr: int) -> None:
        self._code.ProgramSection.collection[program_index].args[0] = target_addr

    def _define_constant(self, name: str, value: int) -> None:
        if name in self._const:
            raise ValueError(f"Constant already defined: {name}")
        if name in self._vars:
            raise ValueError(f"Name already used by variable: {name}")
        if name in self._func:
            raise ValueError(f"Name already used by function: {name}")

        self._const[name] = self._literal_addr(value)

    def _define_allot(self, name: str, size: int) -> None:
        if name in self._vars:
            raise ValueError(f"Variable already defined: {name}")
        if name in self._const:
            raise ValueError(f"Name already used by constant: {name}")
        if name in self._func:
            raise ValueError(f"Name already used by function: {name}")

        self._vars[name] = self._data.alloc(size=size)

    def _compile_if(self) -> None:
        """
        if ... [else ...] then
        Условие берём с вершины стека, 0 -> false, non-zero -> true.
        JZ поглащает predicate со стека.
        """
        jz_index = self._code.ProgramSection.ptr
        self._emit(Opcode.JZ, [0])

        stop = self._parse_until(stop_tokens={"else", "then"})
        if stop == "else":
            jmp_after_true_index = self._code.ProgramSection.ptr
            self._emit(Opcode.JMP, [0])

            else_addr = self._code.program_ptr_byte_addr
            self._patch_program_jump(jz_index, else_addr)

            end_stop = self._parse_until(stop_tokens={"then"})
            if end_stop != "then":
                raise ValueError("Expected 'then' after 'else'")

            end_addr = self._code.program_ptr_byte_addr
            self._patch_program_jump(jmp_after_true_index, end_addr)
            return

        # stop == "then"
        end_addr = self._code.program_ptr_byte_addr
        self._patch_program_jump(jz_index, end_addr)

    def _compile_do_loop(self) -> None:
        """
        do ... loop
        Минимальная семантика: проверка условия перед каждой итерацией.
        Ожидается, что перед `do` и в конце тела цикла на стеке лежит predicate.
        JZ поглащает predicate со стека.
        """
        loop_check_addr = self._code.program_ptr_byte_addr
        jz_exit_index = self._code.ProgramSection.ptr
        self._emit(Opcode.JZ, [0])

        loop_stop = self._parse_until(stop_tokens={"loop"})
        if loop_stop != "loop":
            raise ValueError("Expected 'loop' to close 'do'")

        self._emit(Opcode.JMP, [loop_check_addr])
        loop_exit_addr = self._code.program_ptr_byte_addr
        self._patch_program_jump(jz_exit_index, loop_exit_addr)

    def _define_function(self) -> None:
        """
        Парсинг функций и обработчиков прерываний

        Вынес наружу, ибо вложенность получается дикая
        """
        func_name = self._next_token("Expected function name after ':'")

        if func_name in self._func:
            raise ValueError(f"Function already defined: {func_name}")
        if func_name in self._vars:
            raise ValueError(f"Name already used by variable: {func_name}")
        if func_name in self._const:
            raise ValueError(f"Name already used by constant: {func_name}")

        # Парсинг обработчиков прерываний
        # Ожидаем interruption_[номер прерывания]
        # При том номер прерывания ожидается от 0 до 7
        interrupt_number: Optional[int] = None
        int_match = re.fullmatch(r"interruption_(\d+)", func_name)
        if int_match is not None:
            parsed_num = int(int_match.group(1))
            if not 0 <= parsed_num < INTERRUPT_COUNT:
                raise ValueError(
                    f"Interrupt handler index out of range: {parsed_num} (expected 0..{INTERRUPT_COUNT - 1})"
                )
            if parsed_num in self._interrupt_vectors:
                raise ValueError(
                    f"Interrupt handler already defined: interruption_{parsed_num}"
                )
            interrupt_number = parsed_num
        is_interruption = interrupt_number is not None

        jmp_index = self._code.ProgramSection.ptr

        # Заглушка для перепрыгивания функции
        self._emit(Opcode.JMP, [0])

        # Запоминаем указатель на функцию
        func_addr = self._code.program_ptr_byte_addr

        # Если прерывание - в мапу прерываний и добавляем отключение прерывания внутри
        if is_interruption:
            self._interrupt_vectors[interrupt_number] = func_addr
        # Если обычная функция - в мапу функций
        else:
            self._func[func_name] = func_addr

        stop = self._parse_until(stop_tokens={";"})
        if stop != ";":
            raise ValueError("Expected ';' after function body")

        # Снова разрешаем прерывания
        if is_interruption:
            self._emit(Opcode.IRET)
        else:
            self._emit(Opcode.RET)

        next_instruction_addr = self._code.program_ptr_byte_addr
        self._code.ProgramSection.collection[jmp_index].args[0] = next_instruction_addr

    def _parse_token(self, token: str) -> None:
        """
        Парсинг токена

        Args:
            token (str): обрабатываемый токен
        """
        if token == "if":
            self._compile_if()
            return

        if token == "do":
            self._compile_do_loop()
            return

        if token in {"else", "then", "loop", ";"}:
            raise ValueError(f"Unexpected control token: {token}")

        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            cstring_addr = self._cstring_addr(token[1:-1])

            # Forth-like: "..." constant NAME
            if self._peek_token() == Term.CCR.value:
                self._cursor += 1
                const_name = self._next_token("Expected constant name after 'constant'")
                self._define_constant(const_name, cstring_addr)
                return

            self._emit(Opcode.PUSH, [AddressingMode.IMM.value, cstring_addr])
            return

        const_addr = self._const.get(token)
        if const_addr is not None:
            self._emit(Opcode.PUSH, [AddressingMode.MEM.value, const_addr])
            return

        if token in self._func:
            self._emit(Opcode.CALL, [self._func[token]])
            return

        # Если это название переменной: в Forth переменная кладёт адрес.
        var_addr = self._vars.get(token)
        if var_addr is not None:
            self._emit(Opcode.PUSH, [AddressingMode.IMM.value, var_addr])
            return

        # Обработка литералов
        int_value = self._parse_int(token)
        if int_value is not None:
            # Forth-like: 32 allot BUF
            if self._peek_token() == Term.ALLOT.value:
                self._cursor += 1
                block_name = self._next_token("Expected block name after 'allot'")
                self._define_allot(block_name, int_value)
                return

            # Forth-like: 10 constant TEN
            if self._peek_token() == Term.CCR.value:
                self._cursor += 1
                const_name = self._next_token("Expected constant name after 'constant'")
                self._define_constant(const_name, int_value)
                return

            self._emit(
                Opcode.PUSH, [AddressingMode.MEM.value, self._literal_addr(int_value)]
            )
            return

        # Получаем терм
        term = self._to_term(token)

        # Загрузка по адресу со стека: (addr -- value)
        if term == Term.VLD:
            self._emit(Opcode.PUSH, [AddressingMode.STI.value, 0])
            return

        # Сохранение по адресу со стека: (value addr --)
        if term == Term.VST:
            self._emit(Opcode.POP, [AddressingMode.STI.value, 0])
            return

        # Создание переменной
        if term == Term.VCR:
            var_name = self._next_token("Expected variable name after 'variable'")

            if var_name in self._vars:
                raise ValueError(f"Variable already defined: {var_name}")
            if var_name in self._const:
                raise ValueError(f"Name already used by constant: {var_name}")
            if var_name in self._func:
                raise ValueError(f"Name already used by function: {var_name}")

            self._vars[var_name] = self._data.alloc(size=1)
            return

        if term == Term.CCR:
            raise ValueError(
                "Constant declaration must use literal form: `<value> constant <name>`"
            )

        if term == Term.ALLOT:
            raise ValueError(
                "Allot declaration must use literal form: `<size> allot <name>`"
            )

        # Создание функции
        if term == Term.FCR:
            self._define_function()
            return

        # Пуш на стек execution token-a
        if term == Term.XT:
            func_name = self._next_token("Expected function name after '''")

            if func_name not in self._func:
                raise ValueError(f"Unknown function: {func_name}")
            func_ptr = self._func[func_name]

            self._emit(Opcode.PUSH, [AddressingMode.IMM.value, func_ptr])
            return

        # Вызов прерывания
        if term == Term.INT:
            self._emit(Opcode.INT)
            return

        # Вызов IN PMIO
        if term == Term.PIN:
            self._emit(Opcode.IN)
            return

        # Вызов OUT PMIO
        if term == Term.POUT:
            self._emit(Opcode.OUT)
            return

        # Вызов Execute (для работы с XT)
        if term == Term.EXEC:
            self._emit(Opcode.SCALL)
            return

        # Конец программы (bye)
        if term == Term.BYE:
            self._emit(Opcode.HALT)
            return

        # Инкремент значения на вершине стека
        if term == Term.INC:
            self._emit(Opcode.PUSH, [AddressingMode.MEM.value, self._literal_addr(1)])
            self._emit(Opcode.SPLS)
            return

        # Декремент значения на вершине стека
        if term == Term.DEC:
            self._emit(Opcode.PUSH, [AddressingMode.MEM.value, self._literal_addr(1)])
            self._emit(Opcode.SMIN)
            return

        # Мапа операций над стеком
        stack_arith_map: dict[Term, Opcode] = {
            Term.PLS: Opcode.SPLS,
            Term.MINUS: Opcode.SMIN,
            Term.DIV: Opcode.SDIV,
            Term.MUL: Opcode.SMUL,
        }
        mapped_opcode = stack_arith_map.get(term)
        if mapped_opcode is not None:
            self._emit(mapped_opcode)
            return

        opcode = Opcode[term.name]
        self._emit(opcode)

    def _to_binary(self) -> tuple[bytes, bytes]:
        """
        Упаковка кода и данных в байтовый формат

        Returns:
            tuple[bytes, bytes]: Память программы и память данных соответственно
        """
        # [LD A0][LD A1][entry jmp][interrupt vector table][program]
        entry_jmp_target = self._code.entry_prefix_size
        data_stack_start = self._data.ptr
        return_stack_start = data_stack_start + 1024

        code_bin = (
            to_bytes(Opcode.LD.value, 1)
            + to_bytes(Registers.A0.value, 1)
            + to_bytes(data_stack_start, 3)
            + to_bytes(Opcode.LD.value, 1)
            + to_bytes(Registers.A1.value, 1)
            + to_bytes(return_stack_start, 3)
            + to_bytes(Opcode.JMP.value, 1)
            + to_bytes(entry_jmp_target, 3)
        )

        for idx in range(INTERRUPT_COUNT):
            vector_addr = self._interrupt_vectors.get(idx, 0)
            code_bin += to_bytes(vector_addr, 4)

        for instruction in self._code.ProgramSection.collection:
            opcode = instruction.opcode.value
            args_sizes = Args[instruction.opcode.name].value

            if len(instruction.args) != len(args_sizes):
                raise ValueError(
                    f"Instruction {instruction.opcode.name} expects {len(args_sizes)} args, "
                    f"got {len(instruction.args)}"
                )

            code_bin += to_bytes(opcode, 1)

            for arg, size in zip(instruction.args, args_sizes):
                code_bin += to_bytes(arg, size)

        data_bin = b"".join(to_bytes(elem.value, 5) for elem in self._data.collection)
        return code_bin, data_bin

    def __call__(self) -> None:
        self._parse_until()
        code_bin, data_bin = self._to_binary()

        with open("./exec_code", "wb") as file_obj:
            file_obj.write(code_bin)

        with open("./exec_data", "wb") as file_obj:
            file_obj.write(data_bin)


if __name__ == "__main__":
    Translator(
        """
1 constant flag

flag @ if
    1
else
    2 3 out
then
    3 do  
        1 3 out
        1 3 out
        1 3 out
        3 3 out
        1-
    loop

bye
        """
    )()
