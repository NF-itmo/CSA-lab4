from Config import Opcode, AddressingMode, Args, INTERRUPT_COUNT, instruction_size
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Type
import argparse


WORD_MASK = (1 << 32) - 1
DATA_WORD_SIZE = 5
CODE_WORD_SIZE = 5


def to_i32(value: int) -> int:
    '''
        Эмуляция знакового int32

        Args:
            value (int): python-число

        Returns:
            int:
    '''
    return ((value + (1 << 31)) % (1 << 32)) - (1 << 31)


class Component(metaclass=ABCMeta):
    """Базовый класс компонентов"""

    def __init__(self, name: str, is_instant: bool = False):
        self._name = name
        self._is_instant = is_instant

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_instant(self) -> bool:
        return self._is_instant

    @abstractmethod
    def tick(self) -> None:
        ...

    def __repr__(self) -> str:
        return self._name


class Socket:
    """Сокет коммутации для трассируемой передачи сигналов"""

    def __init__(
        self,
        owner: Component,
        name: str,
        is_input: bool = False,
        is_output: bool = False,
    ) -> None:
        self._owner = owner
        self._name = name
        self._is_input = is_input
        self._is_output = is_output
        self._value = 0

    @property
    def value(self) -> int:
        return self._value

    def set(self, value: int) -> None:
        self._value = value

    def __rshift__(self, other: "Socket") -> "Socket":
        GLOBAL_NETLIST.connect(self, other)
        return other

    def __repr__(self) -> str:
        return f"{self._owner.name}.{self._name}={self._value}"


class Wire:
    """Однонаправленное соединение между сокетами"""

    def __init__(self, src: Socket, dst: Socket):
        self.src = src
        self.dst = dst

    def propagate(self) -> None:
        self.dst.set(self.src.value)


class Netlist:
    """Простой netlist"""

    def __init__(self) -> None:
        self.wires: list[Wire] = []
        self.components: list[Component] = []
        self.tick_counter = 0

    def connect(self, src_port: Socket, dst_port: Socket) -> Wire:
        wire = Wire(src_port, dst_port)
        self.wires.append(wire)
        return wire

    def add_component(self, component: Component) -> None:
        self.components.append(component)

    def _settle(self, limit: int = 20) -> None:
        """
            Распространяет сигнал,
            пока схема не стабилизируется.
        """

        for _ in range(limit):
            changed = False

            # propagate wires
            old_values = [
                (wire.src.value, wire.dst.value)
                for wire in self.wires
            ]

            for wire in self.wires:
                wire.propagate()

            # tick instant components
            for component in self.components:
                if component.is_instant:
                    component.tick()

            # проверяем изменилось ли что-нибудь
            for (old_src, old_dst), wire in zip(old_values, self.wires):
                if old_src != wire.src.value or old_dst != wire.dst.value:
                    changed = True
                    break

            if not changed:
                return

        raise RuntimeError(
            "Netlist did not stabilize (possible combinational loop)"
        )

    def tick(self) -> None:
        self._settle()

        for component in self.components:
            component.tick()

        self._settle()

        self.tick_counter += 1


# Глобальный контекст
# TODO: Возможно это плохо, подумать об этом потом
GLOBAL_NETLIST = Netlist()

# Компоненты ЭВМ


class Register(Component):
    """Регистр с сокетами input/output/latch"""

    def __init__(self, name: str, size: int = 32, default: int = 0) -> None:
        super().__init__(name)
        self._size = size
        self._mask = (1 << size) - 1
        self._value = default & self._mask

        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self.latch = Socket(self, "latch", is_input=True)

    def tick(self) -> None:
        if self.latch.value == 0:
            self._value = self.input.value & self._mask
        self.output.set(self._value)

    def __repr__(self) -> str:
        return f"{self.name}: 0x{self._value:x}"


class MuxN(Component):
    """N-to-1 мультиплексор"""

    def __init__(self, name: str, n: int = 2) -> None:
        super().__init__(name, is_instant=True)
        self.inputs = [
            Socket(self, f"input{i}", is_input=True)
            for i in range(n)
        ]
        self.sel = Socket(self, "sel", is_input=True)
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        self.output.set(self.inputs[self.sel.value].value)


class OrN(Component):
    """OR для объединения линий IRQ"""

    def __init__(self, name: str, n: int) -> None:
        super().__init__(name, is_instant=True)
        self.inputs = [
            Socket(self, f"input{i}", is_input=True)
            for i in range(n)
        ]
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        self.output.set(1 if any(input_.value == 1 for input_ in self.inputs) else 0)


class ALU(Component):
    """АЛУ: арифметика и сравнения"""

    OP_ADD = 0b000
    OP_SUB = 0b001
    OP_MUL = 0b010
    OP_DIV = 0b011
    OP_INC = 0b100
    OP_DEC = 0b101
    OP_EQ =  0b110
    OP_GT =  0b111
    OP_LT =  0b1000
    OP_PASS_A = 0b1001

    def __init__(self) -> None:
        super().__init__("ALU", is_instant=True)
        self.input_a = Socket(self, "input_a", is_input=True)
        self.input_b = Socket(self, "input_b", is_input=True)
        self.operation = Socket(self, "operation", is_input=True)
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        a = to_i32(self.input_a.value)
        b = to_i32(self.input_b.value)
        op = self.operation.value

        if op == self.OP_ADD:
            result = to_i32(a + b)
        elif op == self.OP_SUB:
            result = to_i32(a - b)
        elif op == self.OP_MUL:
            result = to_i32(a * b)
        elif op == self.OP_DIV:
            if b == 0:
                raise ZeroDivisionError("Division by zero")
            result = to_i32(int(a / b))
        elif op == self.OP_INC:
            result = to_i32(a + 1)
        elif op == self.OP_DEC:
            result = to_i32(a - 1)
        elif op == self.OP_EQ:
            result = 1 if a == b else 0
        elif op == self.OP_GT:
            result = 1 if a > b else 0
        elif op == self.OP_LT:
            result = 1 if a < b else 0
        elif op == self.OP_PASS_A:
            result = a
        else:
            raise ValueError(f"Unknown ALU operation: {op}")

        self.output.set(result & WORD_MASK)


class CodeMemory(Component):
    def __init__(self) -> None:
        super().__init__("CodeMem")
        self.addr = Socket(self, "addr", is_input=True)
        self.output = Socket(self, "output", is_output=True)

        self._data: bytes = b""

    def load(self, data: bytes) -> None:
        self._data = data + bytes([Opcode.HALT.value])

    def tick(self) -> None:
        if self.addr.value >= len(self._data):
            raw = bytes([Opcode.HALT.value]).ljust(CODE_WORD_SIZE, b"\x00")
            self.output.set(int.from_bytes(raw, byteorder="big"))
            return

        raw = self._data[self.addr.value:self.addr.value + CODE_WORD_SIZE]
        self.output.set(int.from_bytes(raw.ljust(CODE_WORD_SIZE, b"\x00"), byteorder="big"))


class Mask(Component):
    def __init__(self, mask: int) -> None:
        super().__init__(f"Mask_{mask:x}", is_instant=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self._mask = mask

    def tick(self) -> None:
        self.output.set(
            self.input.value & self._mask
        )

# Маска, но ещё со сдвигом
# TODO: подумать над названием


class ExtractBits(Component):
    def __init__(self, name: str, mask: int, shift: int) -> None:
        super().__init__(name, is_instant=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self._mask = mask
        self._shift = shift

    def tick(self) -> None:
        self.output.set((self.input.value >> self._shift) & self._mask)

# Складыватель для PC


class Adder(Component):
    def __init__(self):
        super().__init__("Adder", is_instant=True)
        self.input = Socket(self, "input", is_input=True)
        self.add_input = Socket(self, "add_input", is_output=True)
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        self.output.set(
            self.input.value + self.add_input.value
        )


class DataMemory(Component):
    def __init__(self):
        super().__init__("DataMem")
        self.addr = Socket(self, "addr", is_input=True)
        self.latch = Socket(self, "latch", is_input=True)
        self.is_write = Socket(self, "is_write", is_input=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)

        self._data: list[int] = []

    def load(self, data: bytes) -> None:
        self._data = []
        for idx in range(0, len(data), DATA_WORD_SIZE):
            chunk = data[idx:idx + DATA_WORD_SIZE]
            if len(chunk) < DATA_WORD_SIZE:
                chunk = chunk.rjust(DATA_WORD_SIZE, b"\x00")
            self._data.append(int.from_bytes(chunk, byteorder="big"))

    @property
    def size(self) -> int:
        return len(self._data)

    def tick(self) -> None:
        if self.latch.value == 0b1:
            if self.is_write.value == 0b1:
                while self.addr.value >= len(self._data):
                    self._data.append(0)
                self._data[self.addr.value] = self.input.value & WORD_MASK
            else:
                if self.addr.value >= len(self._data):
                    self.output.set(0)
                    return
                self.output.set(
                    self._data[self.addr.value]
                )


class ExternalDevice(Component):
    def __init__(self, idx: int):
        super().__init__(f"ExternalDevice{idx}")
        self._idx = idx
        self._irq_requested = False

        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self.irq = Socket(self, "irq", is_output=True)
        self.cs = Socket(self, "cs", is_input=True)
        self.is_write = Socket(self, "is_write", is_input=True)

        self.buffer: list[int] = []
        self.output_buffer: list[int] = []

    def request_interrupt(self, value: int | None = None) -> None:
        if value is not None:
            self.buffer.append(value & 0xFF)
        self._irq_requested = True
        self.irq.set(1)

    def tick(self) -> None:
        self.irq.set(1 if self._irq_requested else 0)

        if self.cs.value == 0b1 and self.is_write.value == 0b1:
            self.output_buffer.append(self.input.value & 0xFF)
            return

        if self.cs.value == 0b1 and self.is_write.value == 0b0:
            value = self.buffer.pop(0) if self.buffer else 0
            self.output.set(value)
            self._irq_requested = False
            self.irq.set(0)


class AdressDecoder(Component):
    def __init__(self, n: int):
        super().__init__("AdressDecoder", is_instant=True)
        self._n = n
        self.addr = Socket(self, "addr", is_input=True)
        self.cs_input = Socket(self, "cs_input", is_input=True)
        self.cs_outputs = [
            Socket(self, f"cs_output{i}", is_output=True)
            for i in range(n)
        ]

    def tick(self) -> None:
        for idx, output in enumerate(self.cs_outputs):
            output.set(1 if self.cs_input.value == 1 and self.addr.value == idx else 0)


class InstructionLogFormatter:
    """Форматирование текущей инструкции для журнала машины"""

    @staticmethod
    def _format_args(opcode: Opcode, args: list[int]) -> str:
        if not args:
            return "-"

        if opcode in {Opcode.JMP, Opcode.JZ, Opcode.JNZ, Opcode.CALL} and len(args) == 1:
            return f"0x{args[0]:x}"

        if opcode in {Opcode.PUSH, Opcode.POP} and len(args) == 2:
            mode_value, operand = args

            try:
                mode = AddressingMode(mode_value).name
            except ValueError:
                mode = f"UNKNOWN({mode_value})"

            return f"mode={mode}, operand=0x{operand:x}"

        return ", ".join(str(hex(arg)) for arg in args)

    def __call__(self, addr: int, instruction: int, instr_size: int) -> str:
        opcode = Opcode((instruction >> (8 * (CODE_WORD_SIZE - 1))) & 0xFF)
        hex_view = (instruction >> (8 * (CODE_WORD_SIZE - instr_size))).to_bytes(
            instr_size,
            byteorder="big",
        ).hex()

        args: list[int] = []
        arg_bits_left = (CODE_WORD_SIZE - 1) * 8
        for arg_size in Args[opcode.name].value:
            arg_bits_left -= arg_size * 8
            args.append((instruction >> arg_bits_left) & ((1 << (arg_size * 8)) - 1))

        return f"{addr:06x} - {hex_view:<14} - {opcode.name:<5} {self._format_args(opcode, args)}"


class CU(Component):
    "Control Unit"

    def __init__(self) -> None:
        super().__init__("CU")

        GLOBAL_NETLIST.wires.clear()
        GLOBAL_NETLIST.components.clear()
        GLOBAL_NETLIST.tick_counter = 0

        self.data_mem = self._create_component(DataMemory)
        self.code_mem = self._create_component(CodeMemory)
        self.external_devices = [
            self._create_component(ExternalDevice, idx)
            for idx in range(INTERRUPT_COUNT)
        ]
        self.device_decoder = self._create_component(AdressDecoder, INTERRUPT_COUNT)
        self.external_output_mux = self._create_component(MuxN, "ExternalOutputMux", INTERRUPT_COUNT)
        self.external_irq_mux = self._create_component(MuxN, "ExternalIrqMux", INTERRUPT_COUNT)
        self.external_irq_or = self._create_component(OrN, "ExternalIrqOr", INTERRUPT_COUNT)
        self.irq = Socket(self, "irq", is_input=True)
        self.irq_device = Socket(self, "irq_device", is_input=True)
        self.interrupt_vector_base = instruction_size(Opcode.LD) * 2 + instruction_size(Opcode.JMP)

        self.d0 = self._create_component(Register, "D0")
        self.d1 = self._create_component(Register, "D1")
        self.d2 = self._create_component(Register, "D2")
        self.a0 = self._create_component(Register, "A0")  # aka DSP - Data Stack Pointer
        self.a1 = self._create_component(Register, "A1")  # aka RSP - Return Stack Pointer
        self.di = self._create_component(Register, "DI", size=1)  # Disable Interrupts

        self.alu_left_mux = self._create_component(MuxN, "AluMuxLeft", 5)
        _ = self.d0.output >> self.alu_left_mux.inputs[0]
        _ = self.d1.output >> self.alu_left_mux.inputs[1]
        _ = self.d2.output >> self.alu_left_mux.inputs[2]
        _ = self.a0.output >> self.alu_left_mux.inputs[3]
        _ = self.a1.output >> self.alu_left_mux.inputs[4]

        self.alu_right_mux = self._create_component(MuxN, "AluMuxRight", 5)
        _ = self.d0.output >> self.alu_right_mux.inputs[0]
        _ = self.d1.output >> self.alu_right_mux.inputs[1]
        _ = self.d2.output >> self.alu_right_mux.inputs[2]
        _ = self.a0.output >> self.alu_right_mux.inputs[3]
        _ = self.a1.output >> self.alu_right_mux.inputs[4]

        self.alu = self._create_component(ALU)
        _ = self.alu_left_mux.output >> self.alu.input_a
        _ = self.alu_right_mux.output >> self.alu.input_b

        self.write_to_register_mux = self._create_component(MuxN, "WriteToReg", 4)
        self.code_arg_write_mask = self._create_component(Mask, 0x0000FFFFFF)
        _ = self.alu.output >> self.write_to_register_mux.inputs[0]
        _ = self.data_mem.output >> self.write_to_register_mux.inputs[1]
        _ = self.code_mem.output >> self.code_arg_write_mask.input
        _ = self.code_arg_write_mask.output >> self.write_to_register_mux.inputs[2]
        _ = self.external_output_mux.output >> self.write_to_register_mux.inputs[3]

        _ = self.write_to_register_mux.output >> self.d0.input
        _ = self.write_to_register_mux.output >> self.d1.input
        _ = self.write_to_register_mux.output >> self.d2.input
        _ = self.write_to_register_mux.output >> self.a0.input
        _ = self.write_to_register_mux.output >> self.a1.input

        self.pc = self._create_component(Register, "PC")

        self.reg_to_data_mem_mux = self._create_component(MuxN, "RegMuxAddr", 5)
        _ = self.d0.output >> self.reg_to_data_mem_mux.inputs[0]
        _ = self.d1.output >> self.reg_to_data_mem_mux.inputs[1]
        _ = self.d2.output >> self.reg_to_data_mem_mux.inputs[2]
        _ = self.a0.output >> self.reg_to_data_mem_mux.inputs[3]
        _ = self.a1.output >> self.reg_to_data_mem_mux.inputs[4]

        self.pc_mask = self._create_component(ExtractBits, "CodePcArg", 0x00FFFFFF, 8)
        self.vector_target_mask = self._create_component(ExtractBits, "CodeVectorTarget", 0xFFFFFFFF, 8)
        self.pc_mux = self._create_component(MuxN, "PCMux", 6)
        self.pc_adder = self._create_component(Adder)
        _ = self.code_mem.output >> self.pc_mask.input
        _ = self.pc_mask.output >> self.pc_mux.inputs[0]
        _ = self.alu.output >> self.pc_mux.inputs[1]
        _ = self.pc.output >> self.pc_adder.input
        _ = self.pc_adder.output >> self.pc_mux.inputs[2]
        _ = self.data_mem.output >> self.pc_mux.inputs[3]
        _ = self.code_mem.output >> self.vector_target_mask.input
        _ = self.vector_target_mask.output >> self.pc_mux.inputs[5]
        _ = self.pc_mux.output >> self.pc.input

        _ = self.pc.output >> self.code_mem.addr

        self.data_mem_addr_mux = self._create_component(MuxN, "DataMemAddrMux", 2)
        self.data_addr_mask = self._create_component(Mask, 0x0000FFFFFF)
        _ = self.code_mem.output >> self.data_addr_mask.input
        _ = self.data_addr_mask.output >> self.data_mem_addr_mux.inputs[0]
        _ = self.reg_to_data_mem_mux.output >> self.data_mem_addr_mux.inputs[1]

        self.data_mem_input_mux = self._create_component(MuxN, "DataMemInputMux", 2)
        _ = self.alu.output >> self.data_mem_input_mux.inputs[0]
        _ = self.pc_adder.output >> self.data_mem_input_mux.inputs[1]

        _ = self.data_mem_addr_mux.output >> self.data_mem.addr
        _ = self.data_mem_input_mux.output >> self.data_mem.input

        for idx, device in enumerate(self.external_devices):
            _ = self.device_decoder.cs_outputs[idx] >> device.cs
            _ = device.output >> self.external_output_mux.inputs[idx]
            _ = device.irq >> self.external_irq_mux.inputs[idx]
            _ = device.irq >> self.external_irq_or.inputs[idx]
            _ = self.reg_to_data_mem_mux.output >> device.input
        _ = self.external_irq_mux.output >> self.irq_device
        _ = self.external_irq_or.output >> self.irq

        self.opcode_input = Socket(self, "CodeMemInput", is_input=True)
        _ = self.code_mem.output >> self.opcode_input

        self.input_buffer = self.external_devices[0].buffer
        self.output_buffer = self.external_devices[0].output_buffer
        self._data_stack_base = 0
        self._return_stack_base = 0
        self._instruction_formatter = InstructionLogFormatter()
        self._current_instruction_log = "------ - -------------- - ----- -"

        self.d0.latch.set(1)                 # Закрываем D0
        self.d1.latch.set(1)                 # Закрываем D1
        self.d2.latch.set(1)                 # Закрываем D2
        self.a0.latch.set(1)                 # Закрываем DSP
        self.a1.latch.set(1)                 # Закрываем RSP
        self.di.latch.set(1)                 # Закрываем DI
        self.pc.latch.set(1)                 # Закрываем PC

    def _create_component[T: Component](self, component: Type[T], *args: ..., **kwargs: ...) -> T:
        _inialized_comonent = component(*args, **kwargs)
        GLOBAL_NETLIST.add_component(_inialized_comonent)

        return _inialized_comonent

    def tick(self) -> None:
        ...

    def load(self, data: bytes, code: bytes):
        self.data_mem.load(data)
        self.code_mem.load(code)
        stack_start = self.data_mem.size
        self._data_stack_base = stack_start
        self._return_stack_base = stack_start + 1024

    def _tick(self) -> None:
        GLOBAL_NETLIST.tick()
        interrupt_mode = "INT" if self.di.output.value == 1 else "---"
        print(
            f"0x{GLOBAL_NETLIST.tick_counter:06x}",
            f"[{interrupt_mode}]",
            end=": "
        )
        print(
            self._current_instruction_log,
        )
        print(
            f"D0: 0x{self.d0.output.value:010x}",
            f"D1: 0x{self.d1.output.value:010x}",
            f"D2: 0x{self.d2.output.value:010x}",
            f"A0: 0x{self.a0.output.value:010x}",
            f"A1: 0x{self.a1.output.value:010x}",
            f"DI: {self.di.output.value}",
            f"PC: 0x{self.pc.output.value:06x}",
            sep=" | ",
        )
        print()

    def print_external_device_buffers(self) -> None:
        print("ExternalDevice buffers:")
        for idx, device in enumerate(self.external_devices):
            print(
                f"DEVICE {idx}:",
                f"input={device.buffer}",
                f"output={device.output_buffer}",
                sep=" | ",
            )

    def run(self, limit: int = 10_000) -> None:
        regs = [self.d0, self.d1, self.d2, self.a0, self.a1]

        for _ in range(limit):
            # Получаем машинное слово команды
            instruction_addr = self.pc.output.value
            self._tick()  # Такт - Code Memory выдаёт окно машинного кода

            # Декодируем opcode и аргументы
            instruction = self.opcode_input.value
            opcode = Opcode((instruction >> (8 * (CODE_WORD_SIZE - 1))) & 0xFF)
            instr_size = instruction_size(opcode)
            self._current_instruction_log = self._instruction_formatter(
                instruction_addr,
                instruction,
                instr_size,
            )

            args: list[int] = []
            arg_bits_left = (CODE_WORD_SIZE - 1) * 8
            for arg_size in Args[opcode.name].value:
                arg_bits_left -= arg_size * 8
                args.append((instruction >> arg_bits_left) & ((1 << (arg_size * 8)) - 1))

            if (
                self.di.output.value == 0
                and self.a1.output.value >= self._return_stack_base
                and self.irq.value == 1
            ):
                # Ищем устройство с активным IRQ
                irq_device = None
                for device_idx in range(INTERRUPT_COUNT):
                    self.external_irq_mux.sel.set(device_idx)    # ExternalIrqMux выбирает IRQ проверяемого устройства
                    self._tick()                                 # Такт - CU видит IRQ выбранного устройства
                    if self.irq_device.value == 1:
                        irq_device = device_idx
                        break

                if irq_device is not None:
                    # Читаем данные устройства IRQ
                    self.device_decoder.addr.set(irq_device)     # AD получает номер устройства с IRQ
                    self.device_decoder.cs_input.set(1)          # AD выдаёт CS устройству с IRQ
                    self.external_devices[irq_device].is_write.set(0)  # ExternalDevice переводится в режим чтения
                    self.external_output_mux.sel.set(irq_device)  # ExternalOutputMux выбирает устройство с IRQ
                    self._tick()                                 # Такт - ExternalDevice выдаёт данные IRQ
                    self.device_decoder.cs_input.set(0)          # AD закрывает CS внешних устройств

                    # Сохраняем данные IRQ в D0
                    self.write_to_register_mux.sel.set(3)        # WriteToReg выбирает выход ExternalDevice
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает данные IRQ
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                    # Кладём данные IRQ на data stack
                    self.alu_left_mux.sel.set(0)                 # Левый вход ALU выбирает D0
                    self.alu.operation.set(self.alu.OP_PASS_A)   # ALU пропускает D0 на вход Data Memory
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из DSP
                    self.data_mem_input_mux.sel.set(0)           # DataMemInputMux выбирает ALU
                    self.data_mem.is_write.set(1)                # Data Memory переводится в режим записи
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self._tick()                                 # Такт - Data Memory пишет данные IRQ на стек
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.data_mem.is_write.set(0)                # Data Memory возвращается в режим чтения

                    # Сохраняем текущий PC и увеличиваем DSP
                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_INC)      # ALU переходит в режим инкремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self.pc_adder.add_input.set(0)               # На ADD подаётся 0 для сохранения текущего PC
                    self.reg_to_data_mem_mux.sel.set(4)          # RegMuxAddr выбирает RSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из RSP
                    self.data_mem_input_mux.sel.set(1)           # DataMemInputMux выбирает текущий PC
                    self.data_mem.is_write.set(1)                # Data Memory переводится в режим записи
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self._tick()                                 # Такт - Data Memory сохраняет текущий PC, DSP++
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.data_mem.is_write.set(0)                # Data Memory возвращается в режим чтения
                    self.data_mem_input_mux.sel.set(0)           # DataMemInputMux выбирает ALU

                    # Увеличиваем RSP после записи return
                    self.alu_left_mux.sel.set(4)                 # Левый вход ALU выбирает RSP
                    self.alu.operation.set(self.alu.OP_INC)      # ALU переходит в режим инкремента RSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a1.latch.set(0)                         # Снимаем защёлку с RSP
                    self._tick()                                 # Такт - RSP++
                    self.a1.latch.set(1)                         # Защёлкиваем RSP

                    # Переходим к ячейке вектора устройства
                    self.pc_mux.inputs[4].set(self.interrupt_vector_base + irq_device * 4)  # CU вычисляет адрес вектора
                    self.pc_mux.sel.set(4)                       # PCMux выбирает адрес ячейки вектора
                    self.pc.latch.set(0)                         # Снимаем защёлку с PC
                    self._tick()                                 # Такт - PC получает адрес вектора
                    self.pc.latch.set(1)                         # Защёлкиваем PC

                    # Читаем адрес обработчика устройства
                    self._tick()                                 # Такт - Code Memory выдаёт окно вектора

                    # Передаём управление обработчику
                    self.pc_mux.sel.set(5)                       # PCMux выбирает адрес обработчика из вектора
                    self.pc.latch.set(0)                         # Снимаем защёлку с PC
                    self.di.input.set(1)                         # На вход DI подаётся запрет прерываний
                    self.di.latch.set(0)                         # Снимаем защёлку с DI
                    self._tick()                                 # Такт - PC получает адрес обработчика, DI=1
                    self.pc.latch.set(1)                         # Защёлкиваем PC
                    self.di.latch.set(1)                         # Защёлкиваем DI
                    continue

            if opcode == Opcode.HALT:
                break

            if opcode == Opcode.NOP:
                # Продвигаем PC на инструкцию
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                           # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - PC получает адрес следующей инструкции
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.JMP:
                # Загружаем PC адресом перехода
                self.pc_mux.sel.set(0)                           # PCMux выбирает адрес из аргумента инструкции
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - PC получает адрес перехода
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.LD:
                # Загружаем immediate в регистр
                target_reg = regs[args[0]]
                self.write_to_register_mux.sel.set(2)            # WriteToReg выбирает immediate из Code Memory
                target_reg.latch.set(0)                          # Снимаем защёлку с выбранного регистра
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                           # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - регистр получает immediate, PC++
                target_reg.latch.set(1)                          # Защёлкиваем выбранный регистр
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.CALL:
                # Сохраняем return в Data Memory[A1]
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.reg_to_data_mem_mux.sel.set(4)              # RegMuxAddr выбирает RSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из RSP
                self.data_mem_input_mux.sel.set(1)               # DataMemInputMux выбирает PC + instr_size
                self.data_mem.is_write.set(1)                    # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory сохраняет адрес возврата
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                    # Data Memory возвращается в режим чтения
                self.data_mem_input_mux.sel.set(0)               # DataMemInputMux выбирает ALU

                # Увеличиваем RSP и переходим
                self.alu_left_mux.sel.set(4)                     # Левый вход ALU выбирает RSP
                self.alu.operation.set(self.alu.OP_INC)          # ALU переходит в режим инкремента RSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a1.latch.set(0)                             # Снимаем защёлку с RSP
                self.pc_mux.sel.set(0)                           # PCMux выбирает адрес функции из инструкции
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - RSP++ и PC получает target
                self.a1.latch.set(1)                             # Защёлкиваем RSP
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.RET or opcode == Opcode.IRET:
                # Уменьшаем RSP до адреса возврата
                if self.a1.output.value <= self._return_stack_base:
                    raise RuntimeError("Return stack underflow")
                self.alu_left_mux.sel.set(4)                     # Левый вход ALU выбирает RSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента RSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a1.latch.set(0)                             # Снимаем защёлку с RSP
                self._tick()                                     # Такт - RSP указывает на адрес возврата
                self.a1.latch.set(1)                             # Защёлкиваем RSP

                # Читаем return из Data Memory[A1]
                self.reg_to_data_mem_mux.sel.set(4)              # RegMuxAddr выбирает RSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из RSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory выдаёт адрес возврата
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.pc_mux.sel.set(3)                           # PCMux выбирает адрес возврата из Data Memory
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                if opcode == Opcode.IRET:
                    self.di.input.set(0)                         # На вход DI подаётся разрешение прерываний
                    self.di.latch.set(0)                         # Снимаем защёлку с DI
                self._tick()                                     # Такт - PC получает адрес возврата, при IRET DI=0
                self.pc.latch.set(1)                             # Защёлкиваем PC
                if opcode == Opcode.IRET:
                    self.di.latch.set(1)                         # Защёлкиваем DI
                continue

            if opcode == Opcode.INT:
                # Запрещаем вложенный INT
                if self.di.output.value == 1:
                    raise RuntimeError("Nested interrupts are disabled")

                # Снимаем номер INT со стека
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на номер INT
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт номер INT
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d0.latch.set(0)                             # Снимаем защёлку с D0
                self._tick()                                     # Такт - D0 получает номер INT
                self.d0.latch.set(1)                             # Защёлкиваем D0

                int_number = self.d0.output.value
                if not 0 <= int_number < INTERRUPT_COUNT:
                    raise RuntimeError(f"Interrupt number out of range: {int_number}")

                # Сохраняем return в Data Memory[A1]
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.reg_to_data_mem_mux.sel.set(4)              # RegMuxAddr выбирает RSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из RSP
                self.data_mem_input_mux.sel.set(1)               # DataMemInputMux выбирает PC + instr_size
                self.data_mem.is_write.set(1)                    # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory сохраняет адрес возврата
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                    # Data Memory возвращается в режим чтения
                self.data_mem_input_mux.sel.set(0)               # DataMemInputMux выбирает ALU

                # Увеличиваем RSP после записи return
                self.alu_left_mux.sel.set(4)                     # Левый вход ALU выбирает RSP
                self.alu.operation.set(self.alu.OP_INC)          # ALU переходит в режим инкремента RSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a1.latch.set(0)                             # Снимаем защёлку с RSP
                self._tick()                                     # Такт - RSP++
                self.a1.latch.set(1)                             # Защёлкиваем RSP

                # Выбираем vector PC по номеру INT
                self.pc_mux.inputs[4].set(self.interrupt_vector_base + int_number * 4)  # CU вычисляет адрес вектора
                self.pc_mux.sel.set(4)                           # PCMux выбирает адрес ячейки вектора INT
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - PC получает адрес вектора
                self.pc.latch.set(1)                             # Защёлкиваем PC

                # Читаем адрес обработчика INT
                self._tick()                                     # Такт - Code Memory выдаёт окно вектора

                # Переходим на обработчик INT
                self.pc_mux.sel.set(5)                           # PCMux выбирает адрес обработчика из вектора
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self.di.input.set(1)                              # На вход DI подаётся запрет прерываний
                self.di.latch.set(0)                              # Снимаем защёлку с DI
                self._tick()                                     # Такт - PC получает адрес обработчика, DI=1
                self.pc.latch.set(1)                             # Защёлкиваем PC
                self.di.latch.set(1)                              # Защёлкиваем DI
                continue

            if opcode == Opcode.SCALL:
                # Сохраняем return перед SCALL
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.reg_to_data_mem_mux.sel.set(4)              # RegMuxAddr выбирает RSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из RSP
                self.data_mem_input_mux.sel.set(1)               # DataMemInputMux выбирает PC + instr_size
                self.data_mem.is_write.set(1)                    # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory сохраняет адрес возврата
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                    # Data Memory возвращается в режим чтения
                self.data_mem_input_mux.sel.set(0)               # DataMemInputMux выбирает ALU
                self.alu_left_mux.sel.set(4)                     # Левый вход ALU выбирает RSP
                self.alu.operation.set(self.alu.OP_INC)          # ALU переходит в режим инкремента RSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a1.latch.set(0)                             # Снимаем защёлку с RSP
                self._tick()                                     # Такт - RSP++
                self.a1.latch.set(1)                             # Защёлкиваем RSP

                # Снимаем execution token со стека
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на вершину стека
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из регистра
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт execution token
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d0.latch.set(0)                             # Снимаем защёлку с D0
                self._tick()                                     # Такт - D0 получает execution token
                self.d0.latch.set(1)                             # Защёлкиваем D0

                # Переходим по execution token
                self.alu_left_mux.sel.set(0)                     # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)       # ALU пропускает execution token без изменения
                self.pc_mux.sel.set(1)                           # PCMux выбирает выход ALU
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - PC получает execution token
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.PUSH:
                addr_mode = AddressingMode(args[0])

                if addr_mode == AddressingMode.IMM:
                    # Берём immediate из Code Memory
                    self.write_to_register_mux.sel.set(2)        # WriteToReg выбирает immediate из Code Memory
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает immediate по проводам
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                elif addr_mode == AddressingMode.MEM:
                    # Читаем значение из Data Memory
                    self.data_mem_addr_mux.sel.set(0)            # DataMemAddrMux выбирает адрес из инструкции
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт слово по адресу инструкции
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает слово из Data Memory
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                elif addr_mode == AddressingMode.STI:
                    # Снимаем адрес косвенной загрузки
                    if self.a0.output.value <= self._data_stack_base:
                        raise RuntimeError("Data stack underflow")

                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)      # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self._tick()                                 # Такт - DSP сдвигается на адрес с вершины стека
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт адрес назначения
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d1.latch.set(0)                         # Снимаем защёлку с D1
                    self._tick()                                 # Такт - D1 получает адрес из стека
                    self.d1.latch.set(1)                         # Защёлкиваем D1

                    # Читаем значение по адресу D1
                    self.reg_to_data_mem_mux.sel.set(1)          # RegMuxAddr выбирает D1 как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт слово по адресу из D1
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает слово из Data Memory
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                elif addr_mode == AddressingMode.REG:
                    # Копируем выбранный регистр в D0
                    self.alu_left_mux.sel.set(args[1])           # Левый вход ALU выбирает регистр-источник
                    self.alu.operation.set(self.alu.OP_PASS_A)   # ALU пропускает выбранный регистр
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает значение выбранного регистра
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                else:
                    raise RuntimeError(f"Unknown addressing mode: {addr_mode}")

                # Записываем D0 на data stack
                self.alu_left_mux.sel.set(0)                     # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)       # ALU пропускает D0 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                    # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory пишет D0 на вершину стека
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                    # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)          # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                           # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode == Opcode.POP:
                addr_mode = AddressingMode(args[0])

                if addr_mode == AddressingMode.STI:
                    # Снимаем адрес назначения STI
                    if self.a0.output.value <= self._data_stack_base:
                        raise RuntimeError("Data stack underflow")
                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)      # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self._tick()                                 # Такт - DSP сдвигается на адрес назначения
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт адрес назначения
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d1.latch.set(0)                         # Снимаем защёлку с D1
                    self._tick()                                 # Такт - D1 получает адрес назначения
                    self.d1.latch.set(1)                         # Защёлкиваем D1

                    # Снимаем сохраняемое значение
                    if self.a0.output.value <= self._data_stack_base:
                        raise RuntimeError("Data stack underflow")

                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)      # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self._tick()                                 # Такт - DSP сдвигается на сохраняемое значение
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт сохраняемое значение
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает сохраняемое значение
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                    # Пишем D0 по адресу D1
                    self.alu_left_mux.sel.set(0)                 # Левый вход ALU выбирает D0
                    self.alu.operation.set(self.alu.OP_PASS_A)   # ALU пропускает D0 на вход Data Memory
                    self.reg_to_data_mem_mux.sel.set(1)          # RegMuxAddr выбирает D1 как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из D1
                    self.data_mem.is_write.set(1)                # Data Memory переводится в режим записи
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.pc_adder.add_input.set(instr_size)      # На ADD подаётся размер текущей инструкции
                    self.pc_mux.sel.set(2)                       # PCMux выбирает PC + instr_size
                    self.pc.latch.set(0)                         # Снимаем защёлку с PC
                    self._tick()                                 # Такт - Data Memory пишет D0 по адресу D1, PC++
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.data_mem.is_write.set(0)                # Data Memory возвращается в режим чтения
                    self.pc.latch.set(1)                         # Защёлкиваем PC
                    continue

                elif addr_mode == AddressingMode.MEM:
                    # Снимаем значение для памяти
                    if self.a0.output.value <= self._data_stack_base:
                        raise RuntimeError("Data stack underflow")

                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)      # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self._tick()                                 # Такт - DSP сдвигается на сохраняемое значение
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт сохраняемое значение
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает сохраняемое значение
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                    # Пишем D0 по адресу инструкции
                    self.alu_left_mux.sel.set(0)                 # Левый вход ALU выбирает D0
                    self.alu.operation.set(self.alu.OP_PASS_A)   # ALU пропускает D0 на вход Data Memory
                    self.data_mem_addr_mux.sel.set(0)            # DataMemAddrMux выбирает адрес из инструкции
                    self.data_mem.is_write.set(1)                # Data Memory переводится в режим записи
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.pc_adder.add_input.set(instr_size)      # На ADD подаётся размер текущей инструкции
                    self.pc_mux.sel.set(2)                       # PCMux выбирает PC + instr_size
                    self.pc.latch.set(0)                         # Снимаем защёлку с PC
                    self._tick()                                 # Такт - Data Memory пишет D0 по адресу инструкции, PC++
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.data_mem.is_write.set(0)                # Data Memory возвращается в режим чтения
                    self.pc.latch.set(1)                         # Защёлкиваем PC
                    continue

                elif addr_mode == AddressingMode.REG:
                    # Снимаем значение для регистра
                    if self.a0.output.value <= self._data_stack_base:
                        raise RuntimeError("Data stack underflow")

                    self.alu_left_mux.sel.set(3)                 # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)      # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                         # Снимаем защёлку с DSP
                    self._tick()                                 # Такт - DSP сдвигается на значение
                    self.a0.latch.set(1)                         # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)          # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)            # DataMemAddrMux выбирает адрес из регистра
                    self.data_mem.is_write.set(0)                # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                   # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)        # WriteToReg выбирает выход Data Memory
                    self._tick()                                 # Такт - Data Memory выдаёт значение
                    self.data_mem.latch.set(0)                   # Data Memory закрывает latch
                    self.d0.latch.set(0)                         # Снимаем защёлку с D0
                    self._tick()                                 # Такт - D0 получает значение со стека
                    self.d0.latch.set(1)                         # Защёлкиваем D0

                    # Пишем D0 в выбранный регистр
                    self.alu_left_mux.sel.set(0)                 # Левый вход ALU выбирает D0
                    self.alu.operation.set(self.alu.OP_PASS_A)   # ALU пропускает D0
                    self.write_to_register_mux.sel.set(0)        # WriteToReg выбирает результат ALU
                    regs[args[1]].latch.set(0)                   # Снимаем защёлку с регистра назначения
                    self.pc_adder.add_input.set(instr_size)      # На ADD подаётся размер текущей инструкции
                    self.pc_mux.sel.set(2)                       # PCMux выбирает PC + instr_size
                    self.pc.latch.set(0)                         # Снимаем защёлку с PC
                    self._tick()                                 # Такт - регистр назначения получает D0, PC++
                    regs[args[1]].latch.set(1)                   # Защёлкиваем регистр назначения
                    self.pc.latch.set(1)                         # Защёлкиваем PC
                    continue

                else:
                    raise RuntimeError(f"Unsupported POP mode: {addr_mode}")

            if opcode in {Opcode.SPLS, Opcode.SMIN, Opcode.SMUL, Opcode.SDIV}:
                # Снимаем правый операнд ALU
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на правый операнд
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт правый операнд
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d0.latch.set(0)                             # Снимаем защёлку с D0
                self._tick()                                     # Такт - D0 получает правый операнд
                self.d0.latch.set(1)                             # Защёлкиваем D0

                # Снимаем левый операнд ALU
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")
                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на левый операнд
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт левый операнд
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d1.latch.set(0)                             # Снимаем защёлку с D1
                self._tick()                                     # Такт - D1 получает левый операнд
                self.d1.latch.set(1)                             # Защёлкиваем D1

                # Считаем результат выбранной операции
                self.alu_left_mux.sel.set(1)                     # Левый вход ALU выбирает D1
                self.alu_right_mux.sel.set(0)                    # Правый вход ALU выбирает D0
                if opcode == Opcode.SPLS:
                    self.alu.operation.set(self.alu.OP_ADD)      # ALU переходит в режим сложения
                elif opcode == Opcode.SMIN:
                    self.alu.operation.set(self.alu.OP_SUB)      # ALU переходит в режим вычитания
                elif opcode == Opcode.SMUL:
                    self.alu.operation.set(self.alu.OP_MUL)      # ALU переходит в режим умножения
                else:
                    self.alu.operation.set(self.alu.OP_DIV)      # ALU переходит в режим деления

                # Кладём результат на stack
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                    # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self._tick()                                     # Такт - Data Memory пишет результат ALU на вершину стека
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                    # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)          # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)          # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                           # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                             # Снимаем защёлку с PC
                self._tick()                                     # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.pc.latch.set(1)                             # Защёлкиваем PC
                continue

            if opcode in {Opcode.EQ, Opcode.GT, Opcode.LT}:
                # Снимаем правый операнд сравнения
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на правый операнд
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт правый операнд
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d0.latch.set(0)                             # Снимаем защёлку с D0
                self._tick()                                     # Такт - D0 получает правый операнд
                self.d0.latch.set(1)                             # Защёлкиваем D0

                # Снимаем левый операнд сравнения
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                     # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)          # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)            # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                             # Снимаем защёлку с DSP
                self._tick()                                     # Такт - DSP сдвигается на левый операнд
                self.a0.latch.set(1)                             # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)              # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                    # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                       # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)            # WriteToReg выбирает выход Data Memory
                self._tick()                                     # Такт - Data Memory выдаёт левый операнд
                self.data_mem.latch.set(0)                       # Data Memory закрывает latch
                self.d1.latch.set(0)                             # Снимаем защёлку с D1
                self._tick()                                     # Такт - D1 получает левый операнд
                self.d1.latch.set(1)                             # Защёлкиваем D1

                # Считаем результат сравнения ALU
                self.alu_left_mux.sel.set(1)                     # Левый вход ALU выбирает D1
                self.alu_right_mux.sel.set(0)                    # Правый вход ALU выбирает D0
                if opcode == Opcode.EQ:
                    self.alu.operation.set(self.alu.OP_EQ)       # ALU переходит в режим сравнения на равенство
                elif opcode == Opcode.GT:
                    self.alu.operation.set(self.alu.OP_GT)       # ALU переходит в режим сравнения greater-than
                else:
                    self.alu.operation.set(self.alu.OP_LT)       # ALU переходит в режим сравнения less-than

                # Кладём boolean на stack
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory пишет результат сравнения на вершину стека
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)           # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                            # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                              # Снимаем защёлку с PC
                self._tick()                                      # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.pc.latch.set(1)                              # Защёлкиваем PC
                continue

            if opcode == Opcode.DUP:
                # Снимаем дублируемое значение
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на дублируемое значение
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт дублируемое значение
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает дублируемое значение
                self.d0.latch.set(1)                              # Защёлкиваем D0

                # Возвращаем первое значение DUP
                self.alu_left_mux.sel.set(0)                      # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)        # ALU пропускает D0 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory восстанавливает первое значение DUP
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP ко второй ячейке
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP указывает на вторую ячейку DUP
                self.a0.latch.set(1)                              # Защёлкиваем DSP

                # Записываем второе значение DUP
                self.alu_left_mux.sel.set(0)                      # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)        # ALU пропускает D0 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory пишет второе значение DUP
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)           # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                            # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                              # Снимаем защёлку с PC
                self._tick()                                      # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.pc.latch.set(1)                              # Защёлкиваем PC
                continue

            if opcode == Opcode.DROP:
                # Просто сдвигаем DSP вниз
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)           # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                            # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                              # Снимаем защёлку с PC
                self._tick()                                      # Такт - DSP-- и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.pc.latch.set(1)                              # Защёлкиваем PC
                continue

            if opcode == Opcode.SWAP:
                # Снимаем первый операнд SWAP
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на первый операнд SWAP
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт первый операнд SWAP
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает первый операнд SWAP
                self.d0.latch.set(1)                              # Защёлкиваем D0

                # Снимаем второй операнд SWAP
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на второй операнд SWAP
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт второй операнд SWAP
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d1.latch.set(0)                              # Снимаем защёлку с D1
                self._tick()                                      # Такт - D1 получает второй операнд SWAP
                self.d1.latch.set(1)                              # Защёлкиваем D1

                # Пишем первый операнд ниже
                self.alu_left_mux.sel.set(0)                      # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)        # ALU пропускает D0 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory пишет первый операнд обратно
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP ко второй ячейке
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP указывает на вторую ячейку SWAP
                self.a0.latch.set(1)                              # Защёлкиваем DSP

                # Пишем второй операнд выше
                self.alu_left_mux.sel.set(1)                      # Левый вход ALU выбирает D1
                self.alu.operation.set(self.alu.OP_PASS_A)        # ALU пропускает D1 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory пишет второй операнд обратно
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)           # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                            # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                              # Снимаем защёлку с PC
                self._tick()                                      # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.pc.latch.set(1)                              # Защёлкиваем PC
                continue

            if opcode == Opcode.JZ or opcode == Opcode.JNZ:
                # Снимаем предикат условного перехода
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на предикат перехода
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт предикат перехода
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает предикат перехода
                self.d0.latch.set(1)                              # Защёлкиваем D0
                predicate = self.d0.output.value
                do_jump = (predicate == 0 and opcode == Opcode.JZ) or (predicate != 0 and opcode == Opcode.JNZ)
                if do_jump:
                    # Загружаем PC адресом перехода
                    self.pc_mux.sel.set(0)                        # PCMux выбирает адрес из аргумента инструкции
                    self.pc.latch.set(0)                          # Снимаем защёлку с PC
                    self._tick()                                  # Такт - PC получает адрес перехода
                    self.pc.latch.set(1)                          # Защёлкиваем PC
                else:
                    # Продвигаем PC на инструкцию
                    self.pc_adder.add_input.set(instr_size)       # На ADD подаётся размер текущей инструкции
                    self.pc_mux.sel.set(2)                        # PCMux выбирает PC + instr_size
                    self.pc.latch.set(0)                          # Снимаем защёлку с PC
                    self._tick()                                  # Такт - PC получает адрес следующей инструкции
                    self.pc.latch.set(1)                          # Защёлкиваем PC
                continue

            if opcode == Opcode.IN:
                # Снимаем номер порта ввода
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на номер порта
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт номер порта
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает номер порта
                self.d0.latch.set(1)                              # Защёлкиваем D0

                # Читаем слово из ExternalDevice
                device_idx = self.d0.output.value & 0xFF
                if not 0 <= device_idx < INTERRUPT_COUNT:
                    raise RuntimeError(f"External device out of range: {device_idx}")

                self.device_decoder.addr.set(device_idx)          # AD получает номер внешнего устройства
                self.device_decoder.cs_input.set(1)               # AD выдаёт CS выбранному устройству
                self.external_devices[device_idx].is_write.set(0)  # ExternalDevice переводится в режим чтения
                self.external_output_mux.sel.set(device_idx)      # ExternalOutputMux выбирает выбранное устройство
                self._tick()                                      # Такт - ExternalDevice выдаёт слово на output
                self.device_decoder.cs_input.set(0)               # AD закрывает CS внешних устройств

                # Сохраняем ввод в D0
                self.write_to_register_mux.sel.set(3)             # WriteToReg выбирает выход ExternalDevice
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает слово устройства ввода
                self.d0.latch.set(1)                              # Защёлкиваем D0

                # Кладём ввод на data stack
                self.alu_left_mux.sel.set(0)                      # Левый вход ALU выбирает D0
                self.alu.operation.set(self.alu.OP_PASS_A)        # ALU пропускает D0 на вход Data Memory
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                     # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self._tick()                                      # Такт - Data Memory пишет слово ввода на стек
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                     # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)           # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)           # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                            # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                              # Снимаем защёлку с PC
                self._tick()                                      # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.pc.latch.set(1)                              # Защёлкиваем PC
                continue

            if opcode == Opcode.OUT:
                # Снимаем номер порта вывода
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                      # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)           # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)             # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                              # Снимаем защёлку с DSP
                self._tick()                                      # Такт - DSP сдвигается на номер порта
                self.a0.latch.set(1)                              # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)               # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                 # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                     # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                        # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)             # WriteToReg выбирает выход Data Memory
                self._tick()                                      # Такт - Data Memory выдаёт номер порта
                self.data_mem.latch.set(0)                        # Data Memory закрывает latch
                self.d0.latch.set(0)                              # Снимаем защёлку с D0
                self._tick()                                      # Такт - D0 получает номер порта
                self.d0.latch.set(1)                              # Защёлкиваем D0

                # Снимаем значение вывода если есть
                if self.a0.output.value > self._data_stack_base:
                    self.alu_left_mux.sel.set(3)                  # Левый вход ALU выбирает DSP
                    self.alu.operation.set(self.alu.OP_DEC)       # ALU переходит в режим декремента DSP
                    self.write_to_register_mux.sel.set(0)         # WriteToReg выбирает результат ALU
                    self.a0.latch.set(0)                          # Снимаем защёлку с DSP
                    self._tick()                                  # Такт - DSP сдвигается на значение вывода
                    self.a0.latch.set(1)                          # Защёлкиваем DSP
                    self.reg_to_data_mem_mux.sel.set(3)           # RegMuxAddr выбирает DSP как адрес Data Memory
                    self.data_mem_addr_mux.sel.set(1)             # DataMemAddrMux выбирает адрес из DSP
                    self.data_mem.is_write.set(0)                 # Data Memory переводится в режим чтения
                    self.data_mem.latch.set(1)                    # Data Memory открывает latch
                    self.write_to_register_mux.sel.set(1)         # WriteToReg выбирает выход Data Memory
                    self._tick()                                  # Такт - Data Memory выдаёт значение вывода
                    self.data_mem.latch.set(0)                    # Data Memory закрывает latch
                    self.d1.latch.set(0)                          # Снимаем защёлку с D1
                    self._tick()                                  # Такт - D1 получает значение вывода
                    self.d1.latch.set(1)                          # Защёлкиваем D1
                    output_value_sel = 1
                else:
                    output_value_sel = 0

                # Пишем значение в ExternalDevice
                device_idx = self.d0.output.value & 0xFF
                if not 0 <= device_idx < INTERRUPT_COUNT:
                    raise RuntimeError(f"External device out of range: {device_idx}")

                self.reg_to_data_mem_mux.sel.set(output_value_sel)  # RegMuxAddr выбирает значение для ExternalDevice
                self.device_decoder.addr.set(device_idx)           # AD получает номер внешнего устройства
                self.device_decoder.cs_input.set(1)                # AD выдаёт CS выбранному устройству
                self.external_devices[device_idx].is_write.set(1)  # ExternalDevice переводится в режим записи
                self._tick()                                       # Такт - ExternalDevice принимает значение
                self.device_decoder.cs_input.set(0)                # AD закрывает CS внешних устройств
                self.external_devices[device_idx].is_write.set(0)  # ExternalDevice возвращается в режим чтения

                # Продвигаем PC после OUT
                self.pc_adder.add_input.set(instr_size)            # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                             # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                               # Снимаем защёлку с PC
                self._tick()                                       # Такт - PC получает адрес следующей инструкции
                self.pc.latch.set(1)                               # Защёлкиваем PC
                continue

            if opcode == Opcode.INC or opcode == Opcode.DEC:
                # Снимаем изменяемое значение
                if self.a0.output.value <= self._data_stack_base:
                    raise RuntimeError("Data stack underflow")

                self.alu_left_mux.sel.set(3)                       # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_DEC)            # ALU переходит в режим декремента DSP
                self.write_to_register_mux.sel.set(0)              # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                               # Снимаем защёлку с DSP
                self._tick()                                       # Такт - DSP сдвигается на изменяемое значение
                self.a0.latch.set(1)                               # Защёлкиваем DSP
                self.reg_to_data_mem_mux.sel.set(3)                # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                  # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(0)                      # Data Memory переводится в режим чтения
                self.data_mem.latch.set(1)                         # Data Memory открывает latch
                self.write_to_register_mux.sel.set(1)              # WriteToReg выбирает выход Data Memory
                self._tick()                                       # Такт - Data Memory выдаёт изменяемое значение
                self.data_mem.latch.set(0)                         # Data Memory закрывает latch
                self.d0.latch.set(0)                               # Снимаем защёлку с D0
                self._tick()                                       # Такт - D0 получает изменяемое значение
                self.d0.latch.set(1)                               # Защёлкиваем D0

                # Выполняем INC или DEC
                self.alu_left_mux.sel.set(0)                       # Левый вход ALU выбирает D0
                if opcode == Opcode.INC:
                    self.alu.operation.set(self.alu.OP_INC)        # ALU переходит в режим инкремента
                else:
                    self.alu.operation.set(self.alu.OP_DEC)        # ALU переходит в режим декремента
                self.reg_to_data_mem_mux.sel.set(3)                # RegMuxAddr выбирает DSP как адрес Data Memory
                self.data_mem_addr_mux.sel.set(1)                  # DataMemAddrMux выбирает адрес из DSP
                self.data_mem.is_write.set(1)                      # Data Memory переводится в режим записи
                self.data_mem.latch.set(1)                         # Data Memory открывает latch
                self._tick()                                       # Такт - Data Memory пишет результат INC/DEC на стек
                self.data_mem.latch.set(0)                         # Data Memory закрывает latch
                self.data_mem.is_write.set(0)                      # Data Memory возвращается в режим чтения

                # Двигаем DSP и PC вперёд
                self.alu_left_mux.sel.set(3)                       # Левый вход ALU выбирает DSP
                self.alu.operation.set(self.alu.OP_INC)            # ALU переходит в режим инкремента DSP
                self.write_to_register_mux.sel.set(0)              # WriteToReg выбирает результат ALU
                self.a0.latch.set(0)                               # Снимаем защёлку с DSP
                self.pc_adder.add_input.set(instr_size)            # На ADD подаётся размер текущей инструкции
                self.pc_mux.sel.set(2)                             # PCMux выбирает PC + instr_size
                self.pc.latch.set(0)                               # Снимаем защёлку с PC
                self._tick()                                       # Такт - DSP++ и PC получает адрес следующей инструкции
                self.a0.latch.set(1)                               # Защёлкиваем DSP
                self.pc.latch.set(1)                               # Защёлкиваем PC
                continue

            raise RuntimeError(f"Unsupported opcode: {opcode}")


def read_file(path: Path) -> bytes:
    with open(path, "rb") as file_obj:
        return file_obj.read()


# Запускаем
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Machine')
    parser.add_argument('-c', '--code', type=Path, required=True, help='Path to code memory dump')
    parser.add_argument('-d', '--data', type=Path, required=True, help='Path to data memory dump')
    parser.add_argument('-s', '--settings', type=Path, required=True, help='Path to settings file')
    args = parser.parse_args()

    cu = CU()
    cu.load(read_file(args.data), read_file(args.data))

    cu.run()
    cu.print_external_device_buffers()
