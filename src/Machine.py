try:
    from .Config import Opcode, AddressingMode, Args, General, instruction_size, LogConfig, MachineConfig
    from .Viewer import Viewer
except ImportError:
    from Config import Opcode, AddressingMode, Args, General, instruction_size, LogConfig, MachineConfig
    from Viewer import Viewer
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Any, Literal
import argparse
import yaml
import re


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

    def add_component[T: Component](self, component: T) -> T:
        self.components.append(component)
        return component

    def _settle(self, limit: int = 10) -> None:
        """
            Распространяет сигнал,
            пока схема не стабилизируется.
        """

        for _ in range(limit):
            changed = False

            # распространяем сигналы
            old_values = [
                (wire.src.value, wire.dst.value)
                for wire in self.wires
            ]

            for wire in self.wires:
                wire.propagate()

            # instant компоненты
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

    def skip(self, t: int = 1) -> None:
        # TODO: это ужасно, переделать
        self.tick_counter += t


# Глобальный контекст
# TODO: Возможно это плохо, подумать об этом потом
GLOBAL_NETLIST = Netlist()


# Компоненты ЭВМ
class Register(Component):
    """Регистр с сокетами input/output/latch"""

    def __init__(
        self,
        name: str,
        size: int = General.DATA_WORD_SIZE,
        default: int = 0,
    ) -> None:
        super().__init__(name)
        self._size = size
        self._mask = (1 << size * 8) - 1
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


class CounterRegister(Component):
    def __init__(
        self,
        name: str,
        size: int = General.DATA_WORD_SIZE
    ) -> None:
        super().__init__(name)
        self._mask = (1 << size * 8) - 1
        self._value = 0

        self.inc = Socket(self, "inc", is_input=True)
        self.reset = Socket(self, "reset", is_input=True)
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        if self.inc.value == 1:
            self._value += 1
        if self.reset.value == 1:
            self._value = 0

        self.output.set(self._value)


class RSTrigger(Component):
    def __init__(self, name: str, default: int = 0):
        super().__init__(name)

        self._value = default

        self.set = Socket(self, "set", is_input=True)
        self.reset = Socket(self, "reset", is_input=True)
        self.output = Socket(self, "output", is_output=True)

    def tick(self) -> None:
        if self.set.value == 1:
            self._value = 1
        if self.reset.value == 1:
            self._value = 0

        self.output.set(self._value)


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

    OP_ADD    = 0b0000
    OP_SUB    = 0b0001
    OP_MUL    = 0b0010
    OP_DIV    = 0b0011
    OP_INC    = 0b0100
    OP_DEC    = 0b0101
    OP_EQ     = 0b0110
    OP_GT     = 0b0111
    OP_LT     = 0b1000
    OP_PASS_A = 0b1001
    OP_ADD_WORD = 0b1010
    OP_SUB_WORD = 0b1011

    def __init__(self) -> None:
        super().__init__("ALU", is_instant=True)

        self.input_a = Socket(self, "input_a", is_input=True)
        self.input_b = Socket(self, "input_b", is_input=True)
        self.operation = Socket(self, "operation", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self.ZF = Socket(self, "ZF", is_output=True)

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
                result = 0
            else:
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
        elif op == self.OP_ADD_WORD:
            result = to_i32(a + General.DATA_WORD_SIZE)
        elif op == self.OP_SUB_WORD:
            result = to_i32(a - General.DATA_WORD_SIZE)
        else:
            raise ValueError(f"Unknown ALU operation: {op}")

        self.ZF.set(result == 0)
        self.output.set(result & General.DATA_WORD_MASK)


class CodeMemory(Component):
    """Память кода: окно CODE_WORD_SIZE байт по байтовому адресу PC."""

    def __init__(self) -> None:
        super().__init__("CodeMem")
        self.addr = Socket(self, "addr", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self._data: bytes = b""

    def load(self, data: bytes) -> None:
        self._data = data + bytes([Opcode.HALT.value])

    @property
    def size(self) -> int:
        return len(self._data)

    def tick(self) -> None:
        if self.addr.value >= len(self._data):
            raw = bytes([Opcode.HALT.value]).ljust(General.CODE_WORD_SIZE, b"\x00")
            self.output.set(int.from_bytes(raw, byteorder="big"))
            return

        raw = self._data[self.addr.value:self.addr.value + General.CODE_WORD_SIZE]
        self.output.set(int.from_bytes(raw.ljust(General.CODE_WORD_SIZE, b"\x00"), byteorder="big"))


class DataMemory(Component):
    """Byte-addressed data memory with 32-bit little-endian word access."""

    def __init__(self) -> None:
        super().__init__("DataMem")
        self.addr = Socket(self, "addr", is_input=True)
        self.latch = Socket(self, "latch", is_input=True)
        self.is_write = Socket(self, "is_write", is_input=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self._data = bytearray()

    def load(self, data: bytes) -> None:
        self._data = bytearray(data)

    @property
    def size(self) -> int:
        return len(self._data)

    def _ensure_size(self, size: int) -> None:
        if size > len(self._data):
            self._data.extend(b"\x00" * (size - len(self._data)))

    def tick(self) -> None:
        if self.latch.value == 1:
            addr = self.addr.value
            if self.is_write.value == 1:
                self._ensure_size(addr + General.DATA_WORD_SIZE)
                raw = (self.input.value & General.DATA_WORD_MASK).to_bytes(
                    General.DATA_WORD_SIZE,
                    byteorder="little",
                )
                self._data[addr:addr + General.DATA_WORD_SIZE] = raw
            else:
                raw = bytes(self._data[addr:addr + General.DATA_WORD_SIZE]).ljust(
                    General.DATA_WORD_SIZE,
                    b"\x00",
                )
                self.output.set(int.from_bytes(raw, byteorder="little"))


@dataclass
class CacheLine:
    valid: bool = False
    dirty: bool = False
    tag: int = 0
    data: bytearray = field(default_factory=bytearray)
    lru_stamp: int = 0


class Cache(Component):
    """
    N-way set-associative cache.

    CPU общается с кэшем через addr/latch/is_write/input/output.
    Кэш общается с нижней памятью только через её сокеты.
    """

    def __init__(
        self,
        name: str,
        storage: CodeMemory | DataMemory,
        read_size: int,
        addr_scale: int,
        read_requires_latch: bool,
        read_only: bool = False,
        enabled: bool = True,
    ):
        super().__init__(name)
        self.addr = Socket(self, "addr", is_input=True)
        self.latch = Socket(self, "latch", is_input=True)
        self.is_write = Socket(self, "is_write", is_input=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)

        self._storage = storage
        self._read_size = read_size
        self._addr_scale = addr_scale
        self._read_requires_latch = read_requires_latch
        self._read_only = read_only
        self._enabled = enabled

        self._line_size = General.CACHE_LINE_SIZE_BYTES
        self._line_count = General.CACHE_LINE_COUNT
        self._way_count = General.CACHE_WAY_COUNT
        self._set_count = self._line_count // General.CACHE_WAY_COUNT
        if self._line_count <= 0 or self._way_count <= 0 or self._set_count <= 0:
            raise ValueError("Invalid cache geometry in config")
        if self._line_count % self._way_count != 0:
            raise ValueError("CACHE_LINE_COUNT must be divisible by CACHE_WAY_COUNT")

        self._lru_clock = 0
        self._sets: list[list[CacheLine]] = []
        self._wait_ticks = 0
        self._pending: Optional[dict[str, Any]] = None
        self._access_count = 0
        self._hit_count = 0
        self._last_served_read_addr: Optional[int] = None
        self._reset_cache()

    def load(self, data: bytes) -> None:
        self._storage.load(data)
        self._reset_cache()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def access_count(self) -> int:
        return self._access_count

    @property
    def hit_count(self) -> int:
        return self._hit_count

    @property
    def miss_count(self) -> int:
        return self._access_count - self._hit_count

    @property
    def hit_rate(self) -> float:
        if self._access_count == 0:
            return 0.0
        return self._hit_count / self._access_count

    @property
    def busy(self) -> bool:
        # Пока busy=True, CU продолжает тикать netlist и ждёт завершения памяти.
        return self._wait_ticks > 0 or self._pending is not None

    def _reset_cache(self) -> None:
        # Инициализация пустых наборов кэша.
        self._sets = [
            [CacheLine(data=bytearray(self._line_size)) for _ in range(self._way_count)]
            for _ in range(self._set_count)
        ]
        self._wait_ticks = 0
        self._pending = None
        self._lru_clock = 0
        self._last_served_read_addr = None
        self._access_count = 0
        self._hit_count = 0

    def _record_access(self, is_hit: bool) -> None:
        self._access_count += 1
        if is_hit:
            self._hit_count += 1

    def _byteorder(self) -> Literal["big", "little"]:
        return "little" if isinstance(self._storage, DataMemory) else "big"

    def _line_base(self, byte_addr: int) -> int:
        return (byte_addr // self._line_size) * self._line_size

    def _range_line_bases(self, byte_addr: int, size: int) -> list[int]:
        first = self._line_base(byte_addr)
        last = self._line_base(byte_addr + size - 1)
        line_bases: list[int] = []
        current = first
        while current <= last:
            line_bases.append(current)
            current += self._line_size
        return line_bases

    def _line_address_to_set_tag(self, line_base: int) -> tuple[int, int]:
        line_number = line_base // self._line_size
        set_idx = line_number % self._set_count
        tag = line_number // self._set_count
        return set_idx, tag

    def _set_tag_to_line_base(self, set_idx: int, tag: int) -> int:
        line_number = tag * self._set_count + set_idx
        return line_number * self._line_size

    def _find_way(self, set_idx: int, tag: int) -> Optional[int]:
        for way_idx, line in enumerate(self._sets[set_idx]):
            if line.valid and line.tag == tag:
                return way_idx
        return None

    def _touch_way(self, set_idx: int, way_idx: int) -> None:
        # LRU: каждое обращение обновляет timestamp линии.
        self._lru_clock += 1
        self._sets[set_idx][way_idx].lru_stamp = self._lru_clock

    def _evict_way(self, set_idx: int) -> int:
        for way_idx, line in enumerate(self._sets[set_idx]):
            if not line.valid:
                return way_idx

        lru_way = min(range(self._way_count), key=lambda idx: self._sets[set_idx][idx].lru_stamp)
        victim = self._sets[set_idx][lru_way]
        # write-back: грязная линия сбрасывается в память только при вытеснении.
        if victim.valid and victim.dirty:
            victim_base = self._set_tag_to_line_base(set_idx, victim.tag)
            self._backend_write_block(victim_base, bytes(victim.data))
            victim.dirty = False
        return lru_way

    def _install_line(self, line_base: int) -> None:
        set_idx, tag = self._line_address_to_set_tag(line_base)
        existing_way = self._find_way(set_idx, tag)
        if existing_way is not None:
            self._touch_way(set_idx, existing_way)
            return

        way_idx = self._evict_way(set_idx)
        line = self._sets[set_idx][way_idx]
        line.valid = True
        line.dirty = False
        line.tag = tag
        line.data = bytearray(self._backend_read_block(line_base, self._line_size))
        self._touch_way(set_idx, way_idx)

    def _backend_read_block(self, start_byte_addr: int, size: int) -> bytes:
        raw = bytearray()
        for offset in range(size):
            cur_byte_addr = start_byte_addr + offset
            if isinstance(self._storage, CodeMemory):
                self._storage.addr.set(cur_byte_addr)  # Подаём байтовый адрес в CodeMemory
                self._storage.tick()                   # Тикаем память кода
                raw_word = self._storage.output.value  # Считываем окно машинного слова
                shift = (General.CODE_WORD_SIZE - 1) * 8
                raw.append((raw_word >> shift) & 0xFF)  # Берём старший байт окна = byte[cur_byte_addr]
                continue

            self._storage.addr.set(cur_byte_addr)     # DataMemory is byte-addressed.
            self._storage.is_write.set(0)
            self._storage.latch.set(1)
            self._storage.tick()
            raw.append(self._storage.output.value & 0xFF)

        return bytes(raw)

    def _backend_write_block(self, start_byte_addr: int, values: bytes) -> None:
        # CodeMemory read-only: запись в неё игнорируется.
        if isinstance(self._storage, CodeMemory):
            return

        for offset, value in enumerate(values):
            cur_byte_addr = start_byte_addr + offset

            self._storage.addr.set(cur_byte_addr)     # DataMemory is byte-addressed.
            self._storage.is_write.set(0)             # Переводим DataMemory в чтение
            self._storage.latch.set(1)                # Открываем latch DataMemory
            self._storage.tick()                      # Тикаем DataMemory для получения слова
            current_word = self._storage.output.value

            updated_word = (current_word & ~0xFF) | (value & 0xFF)

            self._storage.addr.set(cur_byte_addr)
            self._storage.input.set(updated_word)     # Подаём обновлённое слово в DataMemory
            self._storage.is_write.set(1)             # Переводим DataMemory в запись
            self._storage.latch.set(1)                # Открываем latch DataMemory
            self._storage.tick()                      # Тикаем DataMemory для записи слова

    def _has_line(self, line_base: int) -> bool:
        set_idx, tag = self._line_address_to_set_tag(line_base)
        return self._find_way(set_idx, tag) is not None

    def _missing_line_bases(self, byte_addr: int, size: int) -> list[int]:
        return [line_base for line_base in self._range_line_bases(byte_addr, size) if not self._has_line(line_base)]

    def _read_bytes_from_cache(self, byte_addr: int, size: int) -> bytes:
        raw = bytearray()
        for offset in range(size):
            cur_addr = byte_addr + offset
            line_base = self._line_base(cur_addr)
            set_idx, tag = self._line_address_to_set_tag(line_base)
            way_idx = self._find_way(set_idx, tag)
            if way_idx is None:
                raw.append(0)
                continue
            self._touch_way(set_idx, way_idx)
            line = self._sets[set_idx][way_idx]
            raw.append(line.data[cur_addr - line_base])
        return bytes(raw)

    def _write_bytes_to_cache(self, byte_addr: int, values: bytes) -> None:
        for offset, value in enumerate(values):
            cur_addr = byte_addr + offset
            line_base = self._line_base(cur_addr)
            set_idx, tag = self._line_address_to_set_tag(line_base)
            way_idx = self._find_way(set_idx, tag)
            if way_idx is None:
                continue
            line = self._sets[set_idx][way_idx]
            line.data[cur_addr - line_base] = value
            line.dirty = True
            self._touch_way(set_idx, way_idx)

    def _complete_pending(self) -> None:
        # Завершаем отложенную операцию после countdown miss/hit-write задержки.
        if self._pending is None:
            return

        mode = self._pending["mode"]
        byte_addr = self._pending["byte_addr"]
        value_raw = self._pending["value_raw"]
        missing = self._pending["missing"]

        for line_base in missing:
            self._install_line(line_base)

        if mode == "read":
            raw = self._read_bytes_from_cache(byte_addr, self._read_size)
            self.output.set(int.from_bytes(raw, byteorder=self._byteorder()))
            self._last_served_read_addr = byte_addr
        elif mode == "write_hit_alloc":
            self._write_bytes_to_cache(byte_addr, value_raw)
        elif mode == "bypass_read":
            raw = self._backend_read_block(byte_addr, self._read_size)
            self.output.set(int.from_bytes(raw, byteorder=self._byteorder()))
            self._last_served_read_addr = byte_addr
        elif mode == "bypass_write":
            self._backend_write_block(byte_addr, value_raw)
            self._last_served_read_addr = None
        else:
            # write miss + no-write-allocate:
            # 1) пишем сразу в нижнюю память,
            # 2) синхронизируем уже резидентные cache-line (если часть диапазона была hit),
            #    чтобы не оставить "разорванное" слово на границе линий.
            self._backend_write_block(byte_addr, value_raw)
            self._write_bytes_to_cache(byte_addr, value_raw)
            self._last_served_read_addr = None

        self._pending = None

    def _start_pending(self, mode: str, byte_addr: int, value_raw: bytes, missing: list[int]) -> None:
        self._pending = {
            "mode": mode,
            "byte_addr": byte_addr,
            "value_raw": value_raw,
            "missing": missing,
        }
        self._wait_ticks = General.CACHE_MISS_TICKS

    def _tick_bypass(self, do_write: bool, byte_addr: int) -> None:
        if not do_write and self._last_served_read_addr == byte_addr:
            return

        if not do_write:
            self._record_access(False)
            self._start_pending("bypass_read", byte_addr, b"", [])
            return

        value_mask = (1 << (self._read_size * 8)) - 1
        value = self.input.value & value_mask
        value_raw = value.to_bytes(self._read_size, byteorder=self._byteorder())
        self._record_access(False)
        self._start_pending("bypass_write", byte_addr, value_raw, [])

    def tick(self) -> None:
        # Фаза ожидания: эмуляция стоимости доступа к нижней памяти.
        if self._wait_ticks > 0:
            self._wait_ticks -= 1
            if self._wait_ticks == 0:
                self._complete_pending()
            return

        request_active = (self.latch.value == 1) if self._read_requires_latch else True
        if not request_active:
            return

        do_write = self.is_write.value == 1 and not self._read_only
        byte_addr = self.addr.value * self._addr_scale

        if not self._enabled:
            self._tick_bypass(do_write, byte_addr)
            return

        if not do_write:
            if self._last_served_read_addr == byte_addr:
                return

            if isinstance(self._storage, CodeMemory) and byte_addr >= self._storage.size:
                raw = bytes([Opcode.HALT.value]).ljust(self._read_size, b"\x00")
                self.output.set(int.from_bytes(raw, byteorder="big"))
                self._last_served_read_addr = byte_addr
                return

            missing = self._missing_line_bases(byte_addr, self._read_size)
            self._record_access(len(missing) == 0)
            if missing:
                # read miss: ставим отложенную операцию и блокируем CPU на CACHE_MISS_TICKS.
                self._start_pending("read", byte_addr, b"", missing)
                return

            raw = self._read_bytes_from_cache(byte_addr, self._read_size)
            self.output.set(int.from_bytes(raw, byteorder=self._byteorder()))
            self._last_served_read_addr = byte_addr
            return

        self._last_served_read_addr = None
        value_mask = (1 << (self._read_size * 8)) - 1
        value = self.input.value & value_mask
        value_raw = value.to_bytes(self._read_size, byteorder=self._byteorder())
        missing = self._missing_line_bases(byte_addr, self._read_size)
        self._record_access(len(missing) == 0)

        if missing:
            # write miss + no-write-allocate: пишем сразу в память, линию не создаём.
            self._start_pending("write_no_alloc", byte_addr, value_raw, [])
            return

        # write hit + write-back: попадание работает без задержки,
        # меняем только cache-line и помечаем её dirty.
        self._write_bytes_to_cache(byte_addr, value_raw)


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


class SignExtendBits(Component):
    def __init__(self, name: str, bit_count: int, shift: int) -> None:
        super().__init__(name, is_instant=True)
        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self._bit_count = bit_count
        self._shift = shift
        self._mask = (1 << bit_count) - 1
        self._sign_bit = 1 << (bit_count - 1)

    def tick(self) -> None:
        value = (self.input.value >> self._shift) & self._mask
        if value & self._sign_bit:
            value -= 1 << self._bit_count
        self.output.set(value & General.DATA_WORD_MASK)


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


class ExternalDevice(Component):
    def __init__(self, idx: int):
        super().__init__(f"ExternalDevice{idx}")
        self._idx = idx
        self._irq_schedule: list[tuple[int, int]] = []
        self._pending_input: Optional[int] = None

        self.input = Socket(self, "input", is_input=True)
        self.output = Socket(self, "output", is_output=True)
        self.irq = Socket(self, "irq", is_output=True)
        self.cs = Socket(self, "cs", is_input=True)
        self.is_write = Socket(self, "is_write", is_input=True)

        self.output_buffer: list[int] = []

        self.irq.set(0)

    def set_irq_schedule(self, schedule: list[tuple[int, int]]) -> None:
        self._irq_schedule = sorted(schedule, key=lambda pair: pair[0], reverse=True)
        self._pending_input = None

    @property
    def irq_schedule(self) -> list[tuple[int, int]]:
        return self._irq_schedule

    def tick(self) -> None:
        current_tick = GLOBAL_NETLIST.tick_counter
        while len(self._irq_schedule) != 0 and self._irq_schedule[-1][0] <= current_tick:
            _, value = self._irq_schedule.pop()
            self._pending_input = value

        has_ready = self._pending_input is not None
        self.irq.set(1 if has_ready else 0)
        if has_ready:
            self.output.set(self._pending_input if self._pending_input is not None else 0)

        if self.cs.value == 0b1 and self.is_write.value == 0b1:
            self.output_buffer.append(self.input.value)
            return

        if self.cs.value == 0b1 and self.is_write.value == 0b0:
            if has_ready:
                self._pending_input = None


class DeviceAdressDecoder(Component):
    def __init__(self, n: int):
        super().__init__("DeviceAdressDecoder", is_instant=True)

        self._device_cnt = n
        self.addr = Socket(self, "addr", is_input=True)
        self.cs_input = Socket(self, "cs_input", is_input=True)
        self.cs_outputs = [
            Socket(self, f"cs_output_{i}", is_output=True)
            for i in range(self._device_cnt)
        ]

    def tick(self) -> None:
        for idx, output in enumerate(self.cs_outputs):
            output.set(1 if self.cs_input.value == 1 and self.addr.value == idx else 0)


class ExternalDevicesController(Component):
    def __init__(self, n: int = 8) -> None:
        super().__init__("ExternalDevicesController", is_instant=True)

        self.irq = Socket(self, "IRQ", is_output=True)
        self.irq_device = Socket(self, "IRQ_DEVICE", is_output=True)
        self.data_src = Socket(self, "DATA_SRC", is_input=True)
        self.data_out = Socket(self, "DATA_OUT", is_output=True)
        self.device_addr = Socket(self, "DEVICE_ADDR", is_input=True)
        self.device_addr_sel = Socket(self, "DEVICE_ADDR_SEL", is_input=True)
        self.irq_device_sel = Socket(self, "IRQ_DEVICE_SEL", is_input=True)
        self.irq_device_addr = Socket(self, "IRQ_DEVICE_ADDR", is_input=True)
        self.ext_device_req = Socket(self, "EXTERNAL_DEVICE_REQ", is_input=True)
        self.is_write = Socket(self, "IS_WRITE", is_input=True)

        self.device_adress_decoder = GLOBAL_NETLIST.add_component(DeviceAdressDecoder(n))
        _ = self.ext_device_req >> self.device_adress_decoder.cs_input

        self.irq_or = GLOBAL_NETLIST.add_component(OrN("IrqOr", n))
        self.irq_mux = GLOBAL_NETLIST.add_component(MuxN("DevicesIrqMux", n))
        self.device_addr_mux = GLOBAL_NETLIST.add_component(MuxN("DevicesAddrMux", 2))
        self.out_mux = GLOBAL_NETLIST.add_component(MuxN("DevicesOutMux", n))
        self.addr_mask = GLOBAL_NETLIST.add_component(Mask(n - 1))
        _ = self.irq_or.output >> self.irq
        _ = self.irq_mux.output >> self.irq_device
        _ = self.out_mux.output >> self.data_out
        _ = self.device_addr >> self.device_addr_mux.inputs[0]
        _ = self.irq_device_addr >> self.device_addr_mux.inputs[1]
        _ = self.irq_device_sel >> self.irq_mux.sel
        _ = self.device_addr_sel >> self.device_addr_mux.sel
        _ = self.device_addr_mux.output >> self.device_adress_decoder.addr
        _ = self.device_addr_mux.output >> self.addr_mask.input
        _ = self.addr_mask.output >> self.out_mux.sel

        self.external_devices = [
            GLOBAL_NETLIST.add_component(ExternalDevice(idx))
            for idx in range(General.INTERRUPT_COUNT)
        ]

        for idx, device in enumerate(self.external_devices):
            _ = self.device_adress_decoder.cs_outputs[idx] >> device.cs

            _ = device.output >> self.out_mux.inputs[idx]
            _ = device.irq >> self.irq_mux.inputs[idx]

            _ = device.irq >> self.irq_or.inputs[idx]
            _ = self.data_src >> device.input
            _ = self.is_write >> device.is_write

    def tick(self) -> None:
        ...


# Немного синтаксического сахара, чтобы я не сошёл с ума пока пишу
# Де-факто это не влияет никак на исполнение, просто чуть-чуть более приятный синтаксис
class Shugar():
    class Register(Register):
        def __init___(self, name: str):
            super().__init__(name)

        def lock(self):
            self.latch.set(1)

        def unlock(self):
            self.latch.set(0)

    class Mux(MuxN):
        def __init__(self, name: str, n: int):
            super().__init__(name, n)
            self._named_inputs: dict[str, int] = {}

        def register(self, from_: Socket, name: str, idx: int) -> None:
            _ = from_ >> self.inputs[idx]
            self._named_inputs[name] = idx

        def select(self, name: str) -> None:
            self.sel.set(self._named_inputs[name])


class CU2(Component):
    def __init__(self, machine: "Machine"):
        super().__init__("CU")

        # Костыль чтобы стучаться в регистры/защёлки машины
        self._machine = machine

        self.irq = Socket(self, "IRQ", is_input=True)
        self.irq_device = Socket(self, "IRQ_DEVICE", is_input=True)
        self.alu_zero = Socket(self, "ALU_ZERO", is_input=True)
        self.int_addr = Socket(self, "INT_ADDR", is_output=True)
        self.irq_device_sel = Socket(self, "IRQ_DEVICE_SEL", is_output=True)

        self.ir = GLOBAL_NETLIST.add_component(Shugar.Register("INSTRUCTION_REG", size=General.CODE_WORD_SIZE))

        self.step_cnt = GLOBAL_NETLIST.add_component(CounterRegister("STEP_COUNTER"))
        self.di = GLOBAL_NETLIST.add_component(RSTrigger("DI"))
        self.halt = GLOBAL_NETLIST.add_component(RSTrigger("HALT"))
        self.irq_entry = GLOBAL_NETLIST.add_component(RSTrigger("IRQ_ENTRY"))

    def _lock_registers(self) -> None:
        for reg in (
            self.ir,
            self._machine.d0,
            self._machine.d1,
            self._machine.d2,
            self._machine.a0,
            self._machine.a1,
            self._machine.pc,
        ):
            reg.lock()

    def _reset_controls(self) -> None:
        self._lock_registers()
        self.step_cnt.inc.set(0)
        self.step_cnt.reset.set(0)
        self.di.set.set(0)
        self.di.reset.set(0)
        self.halt.set.set(0)
        self.halt.reset.set(0)
        self.irq_entry.set.set(0)
        self.irq_entry.reset.set(0)
        self._machine.data_mem.latch.set(0)
        self._machine.data_mem.is_write.set(0)
        self._machine.devices.ext_device_req.set(0)
        self._machine.devices.is_write.set(0)
        self._machine.devices.device_addr_sel.set(0)

    def _args(self, opcode: Opcode, instruction: int) -> list[int]:
        if opcode == Opcode.POLY:
            return []

        args: list[int] = []
        arg_bits_left = (General.CODE_WORD_SIZE - 1) * 8
        for arg_size in Args[opcode.name].value:
            arg_bits_left -= arg_size * 8
            args.append((instruction >> arg_bits_left) & ((1 << (arg_size * 8)) - 1))
        return args

    def _decode_mode_pair(self, descriptor: int) -> tuple[AddressingMode, AddressingMode]:
        return AddressingMode((descriptor >> 4) & 0xF), AddressingMode(descriptor & 0xF)

    def _is_register_mode(self, mode: AddressingMode) -> bool:
        return mode in {
            AddressingMode.D0,
            AddressingMode.D1,
            AddressingMode.D2,
            AddressingMode.A0,
            AddressingMode.A1,
        }

    def _reg_idx(self, mode: AddressingMode) -> int:
        reg_map = {
            AddressingMode.D0: 0,
            AddressingMode.D1: 1,
            AddressingMode.D2: 2,
            AddressingMode.A0: 3,
            AddressingMode.A1: 4,
        }
        if mode not in reg_map:
            raise RuntimeError(f"Mode is not a register: {mode}")
        return reg_map[mode]

    def _next_step(self) -> None:
        self.step_cnt.inc.set(1)

    def _finish_step(self) -> None:
        self.step_cnt.reset.set(1)

    def _setup_pc_plus(self, instr_size: int) -> None:
        self._machine.pc_adder.add_input.set(instr_size)
        self._machine.pc_mux.sel.set(0)

    def _setup_pc_arg(self) -> None:
        self._machine.pc_mux.sel.set(4)

    def _setup_pc_direct(self, addr: int) -> None:
        self.int_addr.set(addr)
        self._machine.pc_mux.sel.set(5)

    def _setup_pc_mode_arg(self) -> None:
        self._machine.pc_mux.sel.set(6)

    def _setup_pc_from_data(self) -> None:
        self._machine.pc_mux.sel.set(1)

    def _setup_pc_from_vector(self) -> None:
        self._machine.pc_mux.sel.set(3)

    def _setup_pc_from_alu(self) -> None:
        self._machine.pc_mux.sel.set(2)

    def _alu_pass(self, reg_idx: int) -> None:
        self._machine.alu_left_mux.sel.set(reg_idx)
        self._machine.alu.operation.set(self._machine.alu.OP_PASS_A)

    def _alu_inc(self, reg_idx: int) -> None:
        self._machine.alu_left_mux.sel.set(reg_idx)
        self._machine.alu.operation.set(self._machine.alu.OP_INC)

    def _alu_dec(self, reg_idx: int) -> None:
        self._machine.alu_left_mux.sel.set(reg_idx)
        self._machine.alu.operation.set(self._machine.alu.OP_DEC)

    def _alu_inc_word(self, reg_idx: int) -> None:
        self._machine.alu_left_mux.sel.set(reg_idx)
        self._machine.alu.operation.set(self._machine.alu.OP_ADD_WORD)

    def _alu_dec_word(self, reg_idx: int) -> None:
        self._machine.alu_left_mux.sel.set(reg_idx)
        self._machine.alu.operation.set(self._machine.alu.OP_SUB_WORD)

    def _select_reg_from_alu(self) -> None:
        self._machine.write_to_reg_mux.sel.set(0)

    def _select_reg_from_arg(self) -> None:
        self._machine.write_to_reg_mux.sel.set(1)

    def _select_reg_from_data(self) -> None:
        self._machine.write_to_reg_mux.sel.set(2)

    def _select_reg_from_device(self) -> None:
        self._machine.write_to_reg_mux.sel.set(3)

    def _select_reg_from_poly_first_coeff(self) -> None:
        self._machine.write_to_reg_mux.sel.set(4)

    def _select_reg_from_poly_code_coeff(self) -> None:
        self._machine.write_to_reg_mux.sel.set(5)

    def _setup_read_data_from_alu_addr(self) -> None:
        self._machine.data_mem_addr_mux.sel.set(1)
        self._machine.data_mem.is_write.set(0)
        self._machine.data_mem.latch.set(1)

    def _setup_read_data_from_direct_addr(self, addr: int) -> None:
        # Direct data-memory addressing comes from the current instruction operand.
        self._machine.data_mem_addr_mux.sel.set(0)
        self._machine.data_mem.is_write.set(0)
        self._machine.data_mem.latch.set(1)

    def _setup_write_data_from_bypass_to_alu_addr(self, reg_idx: int) -> None:
        self._machine.data_mem_addr_mux.sel.set(1)
        self._machine.data_mem_data_mux.sel.set(1)
        self._machine.bypass_mux.sel.set(reg_idx)
        self._machine.data_mem.is_write.set(1)
        self._machine.data_mem.latch.set(1)

    def _setup_write_data_from_bypass_to_direct_addr(self, addr: int, reg_idx: int) -> None:
        # Direct data-memory addressing comes from the current instruction operand.
        self._machine.data_mem_addr_mux.sel.set(0)
        self._machine.data_mem_data_mux.sel.set(1)
        self._machine.bypass_mux.sel.set(reg_idx)
        self._machine.data_mem.is_write.set(1)
        self._machine.data_mem.latch.set(1)

    def _setup_write_arg_to_alu_addr(self) -> None:
        self._machine.data_mem_addr_mux.sel.set(1)
        self._machine.data_mem_data_mux.sel.set(0)
        self._machine.data_mem.is_write.set(1)
        self._machine.data_mem.latch.set(1)

    def _setup_read_stack_top(self) -> None:
        # A0 points to the current data-stack top.
        self._alu_pass(3)
        self._setup_read_data_from_alu_addr()
        self._select_reg_from_data()

    def _setup_write_bypass_to_stack_top(self, reg_idx: int) -> None:
        self._alu_pass(3)
        self._setup_write_data_from_bypass_to_alu_addr(reg_idx)

    def _setup_write_bypass_to_stack_next(self, reg_idx: int) -> None:
        self._alu_inc_word(3)
        self._select_reg_from_alu()
        self._setup_write_data_from_bypass_to_alu_addr(reg_idx)

    def _setup_write_arg_to_stack_next(self) -> None:
        self._alu_inc_word(3)
        self._select_reg_from_alu()
        self._setup_write_arg_to_alu_addr()

    def _setup_write_return_to_rsp(self, instr_size: int) -> None:
        self._machine.pc_adder.add_input.set(instr_size)
        self._alu_pass(4)
        self._machine.data_mem_addr_mux.sel.set(1)
        self._machine.data_mem_data_mux.sel.set(2)
        self._machine.data_mem.is_write.set(1)
        self._machine.data_mem.latch.set(1)

    def _tick_irq(self, step: int) -> None:
        # Внешний IRQ обслуживается как отдельно между ISA-инструкциями
        if self.di.output.value == 0:
            if step == 1:
                self.irq_device_sel.set(0)
                self._next_step()
                return

            if 2 <= step <= General.INTERRUPT_COUNT + 1:
                selected_idx = step - 2
                if self.irq_device.value == 1:
                    self.di.set.set(1)
                    self.step_cnt.reset.set(1)
                    return

                next_idx = selected_idx + 1
                if next_idx < General.INTERRUPT_COUNT:
                    self.irq_device_sel.set(next_idx)
                    self._next_step()
                else:
                    # IRQ may disappear before the source is sampled; in that case
                    # the entry sequence is cancelled and the interrupted PC is kept.
                    self.irq_entry.reset.set(1)
                    self._finish_step()
                return

            self.irq_entry.reset.set(1)
            self._finish_step()
            return

        irq_device = self.irq_device_sel.value & (General.INTERRUPT_COUNT - 1)
        vector_addr = self._machine.interrupt_vector_base + irq_device * General.INTERRUT_SIZE
        service_step = step + 1

        if service_step == 1:
            self._machine.devices.device_addr_sel.set(1)
            self._machine.devices.ext_device_req.set(1)
            self._select_reg_from_device()
            self._next_step()
        elif service_step == 2:
            self._machine.devices.device_addr_sel.set(1)
            self._machine.devices.ext_device_req.set(1)
            self._select_reg_from_device()
            self._next_step()
        elif service_step == 3:
            self._machine.devices.device_addr_sel.set(1)
            self._machine.devices.ext_device_req.set(1)
            self._select_reg_from_device()
            self._machine.d2.unlock()
            self._next_step()
        elif service_step == 4:
            self._setup_write_bypass_to_stack_next(2)
            self._next_step()
        elif service_step == 5:
            self._machine.a0.unlock()
            self._setup_write_return_to_rsp(0)
            self._next_step()
        elif service_step == 6:
            self._alu_inc_word(4)
            self._select_reg_from_alu()
            self._setup_pc_direct(vector_addr)
            self._next_step()
        elif service_step == 7:
            self._machine.a1.unlock()
            self._machine.pc.unlock()
            self._next_step()
        elif service_step == 8:
            self._setup_pc_from_vector()
            self._next_step()
        elif service_step == 9:
            self._machine.pc.unlock()
            self.irq_entry.reset.set(1)
            self._finish_step()

    def _tick_poly(self, instruction: int, instr_size: int, step: int) -> None:
        degree = (instruction >> (8 * (General.CODE_WORD_SIZE - 2))) & 0xFF

        if degree == 0:
            if step == 1:
                self._select_reg_from_poly_first_coeff()
                self._next_step()
            elif step == 2:
                self._machine.d2.unlock()
                self._next_step()
            elif step == 3:
                self._setup_write_bypass_to_stack_top(2)
                self._setup_pc_plus(instr_size)
                self._next_step()
            elif step == 4:
                self._machine.pc.unlock()
                self._finish_step()
            return

        if step == 1:
            self._setup_read_stack_top()
            self._next_step()
            return

        if step == 2:
            self._select_reg_from_data()
            self._next_step()
            return

        if step == 3:
            self._machine.d0.unlock()
            self._next_step()
            return

        if step == 4:
            high_coeff_addr = self._machine.pc.output.value + 2 + degree * 3
            self._setup_pc_direct(high_coeff_addr)
            self._next_step()
            return

        if step == 5:
            self._machine.pc.unlock()
            self._next_step()
            return

        if step == 6:
            self._select_reg_from_poly_code_coeff()
            self._next_step()
            return

        if step == 7:
            self._machine.d2.unlock()
            self._next_step()
            return

        LOOP_START = 8
        NON_ZERO_DEG_COEF_CNT = degree - 1
        LAST_COEF_BASE = LOOP_START + NON_ZERO_DEG_COEF_CNT * 7
        LOOP_END = LAST_COEF_BASE + 6

        if LOOP_START <= step < LAST_COEF_BASE:
            PHASE = (step - LOOP_START) % 7

            if PHASE == 0:
                self._machine.alu_left_mux.sel.set(2)
                self._machine.alu_right_mux.sel.set(0)
                self._machine.alu.operation.set(self._machine.alu.OP_MUL)
                self._select_reg_from_alu()
            elif PHASE == 1:
                self._machine.d2.unlock()
                coeff_addr = self._machine.pc.output.value - 3
                self._setup_pc_direct(coeff_addr)
            elif PHASE == 2:
                self._machine.pc.unlock()
            elif PHASE == 3:
                self._select_reg_from_poly_code_coeff()
            elif PHASE == 4:
                self._machine.d1.unlock()
            elif PHASE == 5:
                self._machine.alu_left_mux.sel.set(2)
                self._machine.alu_right_mux.sel.set(1)
                self._machine.alu.operation.set(self._machine.alu.OP_ADD)
                self._select_reg_from_alu()
            else:
                self._machine.d2.unlock()

            self._next_step()
            return

        if LAST_COEF_BASE <= step < LOOP_END:
            PHASE_LE = step - LAST_COEF_BASE

            if PHASE_LE == 0:
                self._machine.alu_left_mux.sel.set(2)
                self._machine.alu_right_mux.sel.set(0)
                self._machine.alu.operation.set(self._machine.alu.OP_MUL)
                self._select_reg_from_alu()
            elif PHASE_LE == 1:
                self._machine.d2.unlock()
            elif PHASE_LE == 2:
                self._select_reg_from_poly_first_coeff()
            elif PHASE_LE == 3:
                self._machine.d1.unlock()
            elif PHASE_LE == 4:
                self._machine.alu_left_mux.sel.set(2)
                self._machine.alu_right_mux.sel.set(1)
                self._machine.alu.operation.set(self._machine.alu.OP_ADD)
                self._select_reg_from_alu()
            elif PHASE_LE == 5:
                self._machine.d2.unlock()
            self._next_step()
            return

        TAIL_STEP = step - LOOP_END
        if TAIL_STEP == 0:
            self._setup_write_bypass_to_stack_top(2)
            self._setup_pc_plus(3 * degree)
            self._next_step()
        elif TAIL_STEP == 1:
            self._machine.pc.unlock()
            self._finish_step()

    def tick(self) -> None:
        self._reset_controls()

        if self._machine.code_mem.busy or self._machine.data_mem.busy:
            return

        step = self.step_cnt.output.value
        if self.irq_entry.output.value == 1:
            self._tick_irq(step)
            return

        if step == 0:
            if (
                self.di.output.value == 0
                and self.irq.value == 1
            ):
                self.irq_entry.set.set(1)
                self.step_cnt.inc.set(1)
                return

            # Fetch: слово команды идёт из CodeCache в IR по готовому проводу.
            self.ir.unlock()
            self.step_cnt.inc.set(1)
            return

        if step == 1:
            # IR уже защёлкнул слово на фронте этого такта; следующий шаг декодирует стабильное значение.
            self.ir.lock()
            self.step_cnt.inc.set(1)
            return

        step -= 1

        instruction = self.ir.output.value
        opcode = Opcode((instruction >> (8 * (General.CODE_WORD_SIZE - 1))) & 0xFF)
        args = self._args(opcode, instruction)
        instr_size = instruction_size(opcode, instruction)
        regs = [self._machine.d0, self._machine.d1, self._machine.d2, self._machine.a0, self._machine.a1]

        if opcode == Opcode.HALT:
            self.halt.set.set(1)
            self._finish_step()
            return

        if opcode == Opcode.NOP:
            if step == 1:
                self._setup_pc_plus(instr_size)
                self._next_step()
            else:
                self._machine.pc.unlock()
                self._finish_step()
            return

        if opcode == Opcode.JMP:
            if step == 1:
                self._setup_pc_arg()
                self._next_step()
            else:
                self._machine.pc.unlock()
                self._finish_step()
            return

        if opcode == Opcode.MOV:
            dst_mode, src_mode = self._decode_mode_pair(args[0])
            operand = args[1]

            if dst_mode == AddressingMode.ST_INC:
                if src_mode == AddressingMode.IMM:
                    if step == 1:
                        self._setup_write_arg_to_stack_next()
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 2:
                        self._machine.a0.unlock()
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if self._is_register_mode(src_mode):
                    if step == 1:
                        self._setup_write_bypass_to_stack_next(self._reg_idx(src_mode))
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 2:
                        self._machine.a0.unlock()
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if src_mode in {AddressingMode.MEM, AddressingMode.STI}:
                    if step == 1:
                        if src_mode == AddressingMode.MEM:
                            self._setup_read_data_from_direct_addr(operand)
                        else:
                            self._setup_read_stack_top()
                        self._next_step()
                    elif step == 2:
                        self._select_reg_from_data()
                        self._next_step()
                    elif step == 3:
                        self._machine.d2.unlock()
                        self._next_step()
                    elif step == 4:
                        self._setup_write_bypass_to_stack_next(2)
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 5:
                        self._machine.a0.unlock()
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                raise RuntimeError(f"Unsupported MOV source for +(ST): {src_mode}")

            if dst_mode == AddressingMode.STI:
                if src_mode == AddressingMode.MEMI:
                    if step == 1:
                        self._setup_read_stack_top()
                        self._next_step()
                    elif step == 2:
                        self._select_reg_from_data()
                        self._next_step()
                    elif step == 3:
                        self._machine.d2.unlock()
                        self._next_step()
                    elif step == 4:
                        self._alu_pass(2)
                        self._setup_read_data_from_alu_addr()
                        self._next_step()
                    elif step == 5:
                        self._select_reg_from_data()
                        self._next_step()
                    elif step == 6:
                        self._machine.d2.unlock()
                        self._next_step()
                    elif step == 7:
                        self._setup_write_bypass_to_stack_top(2)
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 8:
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if src_mode == AddressingMode.IMM:
                    if step == 1:
                        self._alu_pass(3)
                        self._setup_write_arg_to_alu_addr()
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 2:
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if self._is_register_mode(src_mode):
                    if step == 1:
                        self._setup_write_bypass_to_stack_top(self._reg_idx(src_mode))
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 2:
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if src_mode == AddressingMode.MEM:
                    if step == 1:
                        self._setup_read_data_from_direct_addr(operand)
                        self._next_step()
                    elif step == 2:
                        self._select_reg_from_data()
                        self._next_step()
                    elif step == 3:
                        self._machine.d2.unlock()
                        self._next_step()
                    elif step == 4:
                        self._setup_write_bypass_to_stack_top(2)
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 5:
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                raise RuntimeError(f"Unsupported MOV source for STI: {src_mode}")

            if dst_mode == AddressingMode.MEMI and src_mode == AddressingMode.ST_DEC:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 5:
                    self._machine.a0.unlock()
                    self._next_step()
                elif step == 6:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 7:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 8:
                    self._machine.d0.unlock()
                    self._next_step()
                elif step == 9:
                    self._alu_pass(2)
                    self._setup_write_data_from_bypass_to_alu_addr(0)
                    self._next_step()
                elif step == 10:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 11:
                    self._machine.a0.unlock()
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if dst_mode == AddressingMode.ST_DEC and src_mode == AddressingMode.STI:
                if step == 1:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 2:
                    self._machine.a0.unlock()
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if dst_mode == AddressingMode.MEM and src_mode in {AddressingMode.STI, AddressingMode.ST_DEC}:
                if src_mode == AddressingMode.STI:
                    if step == 1:
                        self._setup_read_stack_top()
                        self._next_step()
                    elif step == 2:
                        self._select_reg_from_data()
                        self._next_step()
                    elif step == 3:
                        self._machine.d2.unlock()
                        self._next_step()
                    elif step == 4:
                        self._setup_write_data_from_bypass_to_direct_addr(operand, 2)
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    elif step == 5:
                        self._machine.pc.unlock()
                        self._finish_step()
                    return

                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    self._setup_write_data_from_bypass_to_direct_addr(operand, 2)
                    self._next_step()
                elif step == 5:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 6:
                    self._machine.a0.unlock()
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if self._is_register_mode(dst_mode):
                dst_idx = self._reg_idx(dst_mode)
                if step == 1:
                    if src_mode == AddressingMode.IMM:
                        self._select_reg_from_arg()
                        self._setup_pc_plus(instr_size)
                    elif src_mode == AddressingMode.MEM:
                        self._setup_read_data_from_direct_addr(operand)
                        self._select_reg_from_data()
                    elif src_mode == AddressingMode.STI:
                        self._setup_read_stack_top()
                    elif src_mode == AddressingMode.ST_DEC:
                        self._setup_read_stack_top()
                    elif self._is_register_mode(src_mode):
                        self._alu_pass(self._reg_idx(src_mode))
                        self._select_reg_from_alu()
                        self._setup_pc_plus(instr_size)
                    else:
                        raise RuntimeError(f"Unsupported MOV register source: {src_mode}")
                    self._next_step()
                elif step == 2:
                    if src_mode in {AddressingMode.MEM, AddressingMode.STI, AddressingMode.ST_DEC}:
                        self._select_reg_from_data()
                        self._next_step()
                    else:
                        regs[dst_idx].unlock()
                        self._machine.pc.unlock()
                        self._finish_step()
                elif step == 3:
                    regs[dst_idx].unlock()
                    if src_mode == AddressingMode.ST_DEC:
                        self._next_step()
                    else:
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                elif step == 4:
                    if src_mode == AddressingMode.ST_DEC:
                        self._alu_dec_word(3)
                        self._select_reg_from_alu()
                        self._setup_pc_plus(instr_size)
                        self._next_step()
                    else:
                        self._machine.pc.unlock()
                        self._finish_step()
                elif step == 5:
                    if src_mode == AddressingMode.ST_DEC:
                        self._machine.a0.unlock()
                        self._machine.pc.unlock()
                        self._finish_step()
                return

            raise RuntimeError(f"Unsupported MOV combination: {dst_mode}, {src_mode}")

        if opcode == Opcode.CALL:
            mode = AddressingMode(args[0])
            if mode == AddressingMode.IMM:
                if step == 1:
                    self._setup_write_return_to_rsp(instr_size)
                    self._next_step()
                elif step == 2:
                    self._alu_inc_word(4)
                    self._select_reg_from_alu()
                    self._setup_pc_mode_arg()
                    self._next_step()
                elif step == 3:
                    self._machine.a1.unlock()
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if mode == AddressingMode.ST_DEC:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 5:
                    self._machine.a0.unlock()
                    self._setup_write_return_to_rsp(instr_size)
                    self._next_step()
                elif step == 6:
                    self._alu_inc_word(4)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 7:
                    self._machine.a1.unlock()
                    self._alu_pass(2)
                    self._setup_pc_from_alu()
                    self._next_step()
                elif step == 8:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            raise RuntimeError(f"Unsupported CALL mode: {mode}")

        if opcode in {Opcode.RET, Opcode.IRET}:
            if step == 1:
                self._alu_dec_word(4)
                self._setup_read_data_from_alu_addr()
                self._select_reg_from_alu()
                self._next_step()
            elif step == 2:
                self._machine.a1.unlock()
                self._setup_pc_from_data()
                self._next_step()
            elif step == 3:
                self._machine.pc.unlock()
                if opcode == Opcode.IRET:
                    self.di.reset.set(1)
                self._finish_step()
            return

        if opcode == Opcode.INT:
            mode = AddressingMode(args[0])
            if mode != AddressingMode.ST_DEC:
                raise RuntimeError(f"Unsupported INT mode: {mode}")
            if step == 1:
                self._setup_read_stack_top()
                self._next_step()
            elif step == 2:
                self._select_reg_from_data()
                self._next_step()
            elif step == 3:
                self._machine.d2.unlock()
                self._next_step()
            elif step == 4:
                self._alu_dec_word(3)
                self._select_reg_from_alu()
                self._next_step()
            elif step == 5:
                self._machine.a0.unlock()
                self._setup_write_return_to_rsp(instr_size)
                self._next_step()
            elif step == 6:
                int_number = self._machine.d2.output.value
                if not 0 <= int_number < General.INTERRUPT_COUNT:
                    raise RuntimeError(f"Interrupt number out of range: {int_number}")
                vector_addr = self._machine.interrupt_vector_base + (int_number * General.INTERRUT_SIZE)
                self._alu_inc_word(4)
                self._select_reg_from_alu()
                self._setup_pc_direct(vector_addr)
                self._next_step()
            elif step == 7:
                self._machine.a1.unlock()
                self._machine.pc.unlock()
                self._next_step()
            elif step == 8:
                self._setup_pc_from_vector()
                self._next_step()
            elif step == 9:
                self._machine.pc.unlock()
                self.di.set.set(1)
                self._finish_step()
            return

        if opcode in {Opcode.PLS, Opcode.MIN, Opcode.MUL, Opcode.DIV, Opcode.EQ, Opcode.GT, Opcode.LT}:
            dst_mode, src_mode = self._decode_mode_pair(args[0])
            op_map = {
                Opcode.PLS: self._machine.alu.OP_ADD,
                Opcode.MIN: self._machine.alu.OP_SUB,
                Opcode.MUL: self._machine.alu.OP_MUL,
                Opcode.DIV: self._machine.alu.OP_DIV,
                Opcode.EQ: self._machine.alu.OP_EQ,
                Opcode.GT: self._machine.alu.OP_GT,
                Opcode.LT: self._machine.alu.OP_LT,
            }
            self._machine.alu.operation.set(op_map[opcode])

            if dst_mode == AddressingMode.ST_DEC and src_mode == AddressingMode.STI:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d0.unlock()
                    self._next_step()
                elif step == 4:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 5:
                    self._machine.a0.unlock()
                    self._next_step()
                elif step == 6:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 7:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 8:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 9:
                    if opcode == Opcode.DIV and self._machine.d0.output.value == 0:
                        raise ZeroDivisionError("Division by zero")
                    self._machine.alu_left_mux.sel.set(2)
                    self._machine.alu_right_mux.sel.set(0)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 10:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 11:
                    self._setup_write_bypass_to_stack_top(2)
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 12:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if self._is_register_mode(dst_mode) and self._is_register_mode(src_mode):
                dst_idx = self._reg_idx(dst_mode)
                src_idx = self._reg_idx(src_mode)
                self._machine.alu_left_mux.sel.set(dst_idx)
                self._machine.alu_right_mux.sel.set(src_idx)
                if step == 1:
                    if opcode == Opcode.DIV and regs[src_idx].output.value == 0:
                        raise ZeroDivisionError("Division by zero")
                    self._select_reg_from_alu()
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 2:
                    regs[dst_idx].unlock()
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            raise RuntimeError(f"Unsupported {opcode.name} modes: {dst_mode}, {src_mode}")

        if opcode in {Opcode.JZ, Opcode.JNZ}:
            predicate_mode = AddressingMode(args[0])

            if predicate_mode == AddressingMode.ST_DEC:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    self._alu_dec_word(3)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 5:
                    predicate = self._machine.d2.output.value
                    should_jump = (predicate == 0 and opcode == Opcode.JZ) or (predicate != 0 and opcode == Opcode.JNZ)
                    self._machine.a0.unlock()
                    if should_jump:
                        self._setup_pc_mode_arg()
                    else:
                        self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 6:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if predicate_mode == AddressingMode.STI:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    predicate = self._machine.d2.output.value
                    should_jump = (predicate == 0 and opcode == Opcode.JZ) or (predicate != 0 and opcode == Opcode.JNZ)
                    if should_jump:
                        self._setup_pc_mode_arg()
                    else:
                        self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 5:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if self._is_register_mode(predicate_mode):
                predicate = regs[self._reg_idx(predicate_mode)].output.value
                should_jump = (predicate == 0 and opcode == Opcode.JZ) or (predicate != 0 and opcode == Opcode.JNZ)
                if step == 1:
                    if should_jump:
                        self._setup_pc_mode_arg()
                    else:
                        self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 2:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            raise RuntimeError(f"Unsupported {opcode.name} predicate mode: {predicate_mode}")

        if opcode == Opcode.IN:
            mode = AddressingMode(args[0])
            if mode != AddressingMode.STI:
                raise RuntimeError(f"Unsupported IN mode: {mode}")
            if step == 1:
                self._setup_read_stack_top()
                self._next_step()
            elif step == 2:
                self._select_reg_from_data()
                self._next_step()
            elif step == 3:
                self._machine.d2.unlock()
                self._next_step()
            elif step == 4:
                device_idx = self._machine.d2.output.value & 0xFF
                if not 0 <= device_idx < General.INTERRUPT_COUNT:
                    raise RuntimeError(f"External device out of range: {device_idx}")
                self._alu_pass(2)
                self._machine.devices.ext_device_req.set(1)
                self._select_reg_from_device()
                self._next_step()
            elif step == 5:
                self._alu_pass(2)
                self._machine.devices.ext_device_req.set(1)
                self._select_reg_from_device()
                self._next_step()
            elif step == 6:
                self._alu_pass(2)
                self._machine.devices.ext_device_req.set(1)
                self._select_reg_from_device()
                self._machine.d2.unlock()
                self._next_step()
            elif step == 7:
                self._setup_write_bypass_to_stack_top(2)
                self._setup_pc_plus(instr_size)
                self._next_step()
            elif step == 8:
                self._machine.pc.unlock()
                self._finish_step()
            return

        if opcode == Opcode.OUT:
            dev_mode, value_mode = self._decode_mode_pair(args[0])
            if dev_mode != AddressingMode.STI or value_mode != AddressingMode.ST_DEC:
                raise RuntimeError(f"Unsupported OUT modes: {dev_mode}, {value_mode}")
            if step == 1:
                self._setup_read_stack_top()
                self._next_step()
            elif step == 2:
                self._select_reg_from_data()
                self._next_step()
            elif step == 3:
                self._machine.d2.unlock()
                self._next_step()
            elif step == 4:
                self._alu_dec_word(3)
                self._select_reg_from_alu()
                self._next_step()
            elif step == 5:
                self._machine.a0.unlock()
                self._next_step()
            elif step == 6:
                self._setup_read_stack_top()
                self._next_step()
            elif step == 7:
                self._select_reg_from_data()
                self._next_step()
            elif step == 8:
                self._machine.d0.unlock()
                self._next_step()
            elif step == 9:
                device_idx = self._machine.d2.output.value & 0xFF
                if not 0 <= device_idx < General.INTERRUPT_COUNT:
                    raise RuntimeError(f"External device out of range: {device_idx}")
                self._alu_pass(2)
                self._machine.devices.ext_device_req.set(1)
                self._machine.devices.is_write.set(1)
                self._machine.bypass_mux.sel.set(0)
                self._next_step()
            elif step == 10:
                self._alu_dec_word(3)
                self._select_reg_from_alu()
                self._setup_pc_plus(instr_size)
                self._next_step()
            elif step == 11:
                self._machine.a0.unlock()
                self._machine.pc.unlock()
                self._finish_step()
            return

        if opcode in {Opcode.INC, Opcode.DEC}:
            mode = AddressingMode(args[0])
            if mode == AddressingMode.STI:
                if step == 1:
                    self._setup_read_stack_top()
                    self._next_step()
                elif step == 2:
                    self._select_reg_from_data()
                    self._next_step()
                elif step == 3:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 4:
                    if opcode == Opcode.INC:
                        self._alu_inc(2)
                    else:
                        self._alu_dec(2)
                    self._select_reg_from_alu()
                    self._next_step()
                elif step == 5:
                    self._machine.d2.unlock()
                    self._next_step()
                elif step == 6:
                    self._setup_write_bypass_to_stack_top(2)
                    self._setup_pc_plus(instr_size)
                    self._next_step()
                elif step == 7:
                    self._machine.pc.unlock()
                    self._finish_step()
                return

            if not self._is_register_mode(mode):
                raise RuntimeError(f"Unsupported {opcode.name} mode: {mode}")
            if opcode == Opcode.INC:
                self._alu_inc(self._reg_idx(mode))
            else:
                self._alu_dec(self._reg_idx(mode))
            self._select_reg_from_alu()
            self._setup_pc_plus(instr_size)
            if step == 1:
                self._next_step()
            else:
                regs[self._reg_idx(mode)].unlock()
                self._machine.pc.unlock()
                self._finish_step()
            return

        if opcode == Opcode.POLY:
            self._tick_poly(instruction, instr_size, step)
            return

        raise RuntimeError(f"Unsupported opcode: {opcode}")


class Machine():
    def __init__(self, on_tick: Callable[[], None], cache_enabled: bool = True) -> None:
        self._on_tick = on_tick
        self._cache_enabled = cache_enabled

        self._clear_netlist()
        self._init_memory()
        self.interrupt_vector_base = instruction_size(Opcode.MOV) * 2 + instruction_size(Opcode.JMP)

        self.devices = GLOBAL_NETLIST.add_component(ExternalDevicesController())
        self.cu = GLOBAL_NETLIST.add_component(CU2(self))

        _ = self.devices.irq >> self.cu.irq
        _ = self.devices.irq_device >> self.cu.irq_device
        _ = self.cu.irq_device_sel >> self.devices.irq_device_sel
        _ = self.cu.irq_device_sel >> self.devices.irq_device_addr

        self.d0 = GLOBAL_NETLIST.add_component(Shugar.Register("D0"))
        self.d1 = GLOBAL_NETLIST.add_component(Shugar.Register("D1"))
        self.d2 = GLOBAL_NETLIST.add_component(Shugar.Register("D2"))
        self.a0 = GLOBAL_NETLIST.add_component(Shugar.Register("A0"))  # aka DSP - Data Stack Pointer
        self.a1 = GLOBAL_NETLIST.add_component(Shugar.Register("A1"))  # aka RSP - Return Stack Pointer

        self.alu_left_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("ALU_A", n=5))
        _ = self.d0.output >> self.alu_left_mux.inputs[0]
        _ = self.d1.output >> self.alu_left_mux.inputs[1]
        _ = self.d2.output >> self.alu_left_mux.inputs[2]
        _ = self.a0.output >> self.alu_left_mux.inputs[3]
        _ = self.a1.output >> self.alu_left_mux.inputs[4]

        self.alu_right_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("ALU_B", n=5))
        _ = self.d0.output >> self.alu_right_mux.inputs[0]
        _ = self.d1.output >> self.alu_right_mux.inputs[1]
        _ = self.d2.output >> self.alu_right_mux.inputs[2]
        _ = self.a0.output >> self.alu_right_mux.inputs[3]
        _ = self.a1.output >> self.alu_right_mux.inputs[4]

        self.bypass_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("PASS", n=5))
        _ = self.d0.output >> self.bypass_mux.inputs[0]
        _ = self.d1.output >> self.bypass_mux.inputs[1]
        _ = self.d2.output >> self.bypass_mux.inputs[2]
        _ = self.a0.output >> self.bypass_mux.inputs[3]
        _ = self.a1.output >> self.bypass_mux.inputs[4]

        self.alu = GLOBAL_NETLIST.add_component(ALU())
        _ = self.alu_left_mux.output >> self.alu.input_a
        _ = self.alu_right_mux.output >> self.alu.input_b
        _ = self.alu.ZF >> self.cu.alu_zero
        # output будет скоммутирован далее

        # коммутируем адресную шину внеш. устр к ALU - PASS_A должен помочь
        _ = self.alu.output >> self.devices.device_addr

        # Связанное с CODE MEM
        self.pc_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("PC_MUX", n=7))
        self.pc = GLOBAL_NETLIST.add_component(Shugar.Register("PC"))
        self.pc_adder = GLOBAL_NETLIST.add_component(Adder())
        self.pc_remove_opcode_mask = GLOBAL_NETLIST.add_component(ExtractBits("ARG_TO_PC", mask=0x00FFFFFF, shift=8))
        self.pc_vector_mask = GLOBAL_NETLIST.add_component(ExtractBits("VEC_TO_PC", mask=0x00FFFFFF, shift=16))
        self.pc_mode_arg_mask = GLOBAL_NETLIST.add_component(ExtractBits("MODE_ARG_TO_PC", mask=0x000000FFFFFF, shift=0))
        _ = self.pc_mux.output >> self.pc.input
        _ = self.pc.output >> self.pc_adder.input
        _ = self.pc_adder.output >> self.pc_mux.inputs[0]
        _ = self.data_mem.output >> self.pc_mux.inputs[1]
        _ = self.alu.output >> self.pc_mux.inputs[2]

        _ = self.code_mem.output >> self.pc_vector_mask.input
        _ = self.pc_vector_mask.output >> self.pc_mux.inputs[3]

        _ = self.code_mem.output >> self.pc_remove_opcode_mask.input
        _ = self.pc_remove_opcode_mask.output >> self.pc_mux.inputs[4]

        # Микропрограммам INT/IRQ нужен служебный адрес PC
        _ = self.cu.int_addr >> self.pc_mux.inputs[5]

        _ = self.code_mem.output >> self.pc_mode_arg_mask.input
        _ = self.pc_mode_arg_mask.output >> self.pc_mux.inputs[6]

        _ = self.pc.output >> self.code_mem.addr

        # Связанное с DATA MEM
        self.data_mem_addr_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("DADDR", n=2))
        self.data_mem_addr_arg_mask = GLOBAL_NETLIST.add_component(Mask(0x0000FFFFFF))
        _ = self.code_mem.output >> self.data_mem_addr_arg_mask.input
        _ = self.data_mem_addr_arg_mask.output >> self.data_mem_addr_mux.inputs[0]
        _ = self.alu.output >> self.data_mem_addr_mux.inputs[1]
        _ = self.data_mem_addr_mux.output >> self.data_mem.addr

        self.data_mem_data_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("DDATA", n=3))
        self.code_mem_arg_mask = GLOBAL_NETLIST.add_component(Mask(0x0000FFFFFF))
        _ = self.code_mem.output >> self.code_mem_arg_mask.input
        _ = self.code_mem_arg_mask.output >> self.data_mem_data_mux.inputs[0]
        _ = self.bypass_mux.output >> self.data_mem_data_mux.inputs[1]
        _ = self.pc_adder.output >> self.data_mem_data_mux.inputs[2]
        _ = self.data_mem_data_mux.output >> self.data_mem.input

        # Запись в регистры
        self.write_to_reg_mux = GLOBAL_NETLIST.add_component(Shugar.Mux("REG", n=6))
        self.code_mem_arg_mask_2 = GLOBAL_NETLIST.add_component(Mask(0x0000FFFFFF))
        self.poly_first_coeff = GLOBAL_NETLIST.add_component(SignExtendBits("POLY_FIRST_COEFF", 24, 0))
        self.poly_code_coeff = GLOBAL_NETLIST.add_component(SignExtendBits("POLY_CODE_COEFF", 24, 16))
        _ = self.alu.output >> self.write_to_reg_mux.inputs[0]

        _ = self.code_mem.output >> self.code_mem_arg_mask_2.input
        _ = self.code_mem_arg_mask_2.output >> self.write_to_reg_mux.inputs[1]

        _ = self.data_mem.output >> self.write_to_reg_mux.inputs[2]
        _ = self.devices.data_out >> self.write_to_reg_mux.inputs[3]
        _ = self.cu.ir.output >> self.poly_first_coeff.input
        _ = self.poly_first_coeff.output >> self.write_to_reg_mux.inputs[4]
        _ = self.code_mem.output >> self.poly_code_coeff.input
        _ = self.poly_code_coeff.output >> self.write_to_reg_mux.inputs[5]
        _ = self.bypass_mux.output >> self.devices.data_src

        _ = self.write_to_reg_mux.output >> self.d0.input
        _ = self.write_to_reg_mux.output >> self.d1.input
        _ = self.write_to_reg_mux.output >> self.d2.input
        _ = self.write_to_reg_mux.output >> self.a0.input
        _ = self.write_to_reg_mux.output >> self.a1.input

        # Подача инструкции в CU
        _ = self.code_mem.output >> self.cu.ir.input

    def _clear_netlist(self) -> None:
        GLOBAL_NETLIST.wires.clear()
        GLOBAL_NETLIST.components.clear()
        GLOBAL_NETLIST.tick_counter = 0

    def _init_memory(self) -> None:
        # Нижняя память + два кэша поверх неё
        self.data_storage = DataMemory()
        self.code_storage = CodeMemory()

        self.data_mem = GLOBAL_NETLIST.add_component(
            Cache(
                "DataCache",
                self.data_storage,
                General.DATA_WORD_SIZE,
                1,
                True,
                False,
                self._cache_enabled,
            )
        )
        self.code_mem = GLOBAL_NETLIST.add_component(
            Cache(
                "CodeCache",
                self.code_storage,
                General.CODE_WORD_SIZE,
                1,
                False,
                True,
                self._cache_enabled,
            )
        )

    def run(self) -> None:
        while self.cu.halt.output.value != 1:
            GLOBAL_NETLIST.tick()
            self._on_tick()


# Обвязка


class Logger:
    """Логирование выполнения машины согласно конфигу"""

    def __init__(self, log_configs: list[LogConfig], machine: Machine) -> None:
        self.log_configs = log_configs
        self._machine = machine
        self._first_tick_data: Optional[dict[str, int]] = None
        self._last_tick_data: Optional[dict[str, int]] = None
        self._initial_pmio_snapshot: dict[tuple[int, str], list[int]] = {}
        self._has_assert_fail = False
        self._template_detection = re.compile(r"\{([^{}]+)\}")
        self._out: list[str] = []
        self._logs: dict[str, list[str]] = {conf.name: [] for conf in log_configs if conf.slice == 'all'}

    @property
    def out(self) -> str:
        return "".join(self._out)

    def _format_value(
            self,
            value: int,
            fmt: str  # Literal['hex', 'dec', 'bin', 'bool', 'str']
    ) -> str:
        """Форматирует значение согласно строке формата"""
        if fmt == "hex":
            return hex(value)
        elif fmt == "dec":
            return str(value)
        elif fmt == "bin":
            return bin(value)
        elif fmt == "bool":
            return str(bool(value))
        elif fmt == "str":
            return chr(value & 0xFF)
        else:
            raise ValueError("No such value format:", fmt)

    def _format_float(self, value: float, fmt: str) -> str:
        if fmt == "dec":
            return f"{value:.4f}"
        if fmt in {"pct", "percent"}:
            return f"{value * 100:.2f}%"
        return str(value)

    def _format_instruction(self, raw_instruction: int, fmt: str) -> str:
        """Форматирует машинное слово инструкции"""
        raw_masked = raw_instruction & General.CODE_WORD_MASK
        opcode_raw = (raw_masked >> (8 * (General.CODE_WORD_SIZE - 1))) & 0xFF

        if fmt == "hex":
            return f"0x{raw_masked:0{General.CODE_WORD_SIZE * 2}x}"

        try:
            opcode = Opcode(opcode_raw)

            args: list[int] = []
            arg_bits_left = (General.CODE_WORD_SIZE - 1) * 8
            for arg_size in Args[opcode.name].value:
                arg_bits_left -= arg_size * 8
                arg_mask = (1 << (arg_size * 8)) - 1
                args.append((raw_masked >> arg_bits_left) & arg_mask)

            arg_view = Viewer.format_args(opcode, args, data_words=[])
            if fmt == "str":
                return f"{opcode.name:<5} {arg_view}"
            return arg_view
        except ValueError:
            if fmt == "str":
                return f"0x{raw_masked:0{General.CODE_WORD_SIZE * 2}x} - UNKNOWN(0x{opcode_raw:02x})"
            return f"UNKNOWN(0x{opcode_raw:02x})"

    def _substitute_template(
        self,
        template: str,
        tick_data: dict[str, int],
        pmio_snapshot: Optional[dict[tuple[int, str], list[int]]] = None,
    ) -> str:
        """Подставляет значения в шаблон"""
        def replace(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            parts = token.split(":")
            source, fmt = parts[0], parts[-1]

            if source == "pmio":
                device_idx = int(parts[1])
                io_kind = parts[2]
                return self._format_pmio(device_idx, io_kind, fmt, pmio_snapshot)

            if source == "cache":
                cache_name = parts[1]
                metric = parts[2]
                return self._format_cache_metric(cache_name, metric, fmt)

            if source == "instruction":
                return self._format_instruction(tick_data[source], fmt)

            if source in tick_data:
                return self._format_value(tick_data[source], fmt)
            return match.group(0)

        return re.sub(self._template_detection, replace, template)

    def _format_cache_metric(self, cache_name: str, metric: str, fmt: str) -> str:
        caches = {
            "code": [self._machine.code_mem],
            "data": [self._machine.data_mem],
            "all": [self._machine.code_mem, self._machine.data_mem],
            "total": [self._machine.code_mem, self._machine.data_mem],
        }.get(cache_name)
        if caches is None:
            raise RuntimeError(f"Unknown cache metric source: {cache_name}")

        accesses = sum(cache.access_count for cache in caches)
        hits = sum(cache.hit_count for cache in caches)
        misses = sum(cache.miss_count for cache in caches)

        if metric == "hitRate":
            value = 0.0 if accesses == 0 else hits / accesses
            return self._format_float(value, fmt)
        if metric == "accesses":
            return self._format_value(accesses, fmt)
        if metric == "hits":
            return self._format_value(hits, fmt)
        if metric == "misses":
            return self._format_value(misses, fmt)
        if metric == "enabled":
            return self._format_value(1 if all(cache.enabled for cache in caches) else 0, fmt)

        raise RuntimeError(f"Unknown cache metric: {metric}")

    def _format_pmio(
        self,
        device_idx: int,
        io_kind: str,
        fmt: str,
        pmio_snapshot: Optional[dict[tuple[int, str], list[int]]] = None,
    ) -> str:
        if not 0 <= device_idx < len(self._machine.devices.external_devices):
            raise RuntimeError("Unreachable ext device")

        if pmio_snapshot is not None:
            values = pmio_snapshot.get((device_idx, io_kind), [])
        else:
            device = self._machine.devices.external_devices[device_idx]
            if io_kind == "input":
                values = [value for _, value in device.irq_schedule]
            else:
                values = device.output_buffer

        if fmt == "sym":
            return f"\"{''.join(chr(v & 0xFF) for v in values)}\""

        return "[" + ", ".join(self._format_value(v, fmt) for v in values) + "]"

    def _capture_pmio_snapshot(self) -> dict[tuple[int, str], list[int]]:
        snapshot: dict[tuple[int, str], list[int]] = {}
        for idx, device in enumerate(self._machine.devices.external_devices):
            snapshot[(idx, "input")] = [value for _, value in device.irq_schedule]
            snapshot[(idx, "output")] = list(device.output_buffer)
        return snapshot

    def start(self) -> None:
        self._initial_pmio_snapshot = self._capture_pmio_snapshot()
        self._has_assert_fail = False

    def on_tick(self) -> None:
        """Логирует состояние после текущего такта"""
        tick_data: dict[str, int] = {
            "counter": GLOBAL_NETLIST.tick_counter,
            "PC": self._machine.pc.output.value,
            "DI": self._machine.cu.di.output.value,
            "D0": self._machine.d0.output.value,
            "D1": self._machine.d1.output.value,
            "D2": self._machine.d2.output.value,
            "A0": self._machine.a0.output.value,
            "A1": self._machine.a1.output.value,
            "instruction": self._machine.cu.ir.output.value,
        }
        if self._first_tick_data is None:
            self._first_tick_data = dict(tick_data)
        self._last_tick_data = dict(tick_data)

        for log_config in self.log_configs:
            if log_config.slice == "all":
                output = self._substitute_template(log_config.view, tick_data)
                self._logs[log_config.name].append(output)

    def finish(self) -> None:
        """Выводит финальные логи и assert-проверки"""
        final_tick = self._last_tick_data or {}
        # final_pmio_snapshot = self._capture_pmio_snapshot()

        for log_config in self.log_configs:
            _log_part: list[str] = []
            body_out = ""

            if log_config.slice == 'last':
                _log_part.append(f"\n{log_config.name}:\n")
                body_out = self._substitute_template(log_config.view, final_tick)
                _log_part.append(body_out)

            if log_config.slice == 'all':
                _log_part.append(f"\n{log_config.name}:\n")

                if log_config.head is None and log_config.tail is None:
                    body_out = "\n".join(self._logs[log_config.name])
                else:
                    body_parts: list[str] = []
                    if log_config.head is not None:
                        body_parts.append("\n".join(self._logs[log_config.name][:log_config.head]))
                    body_parts.append("...\n")
                    if log_config.tail is not None:
                        body_parts.append("\n".join(self._logs[log_config.name][-log_config.tail:]))
                    body_out = "".join(body_parts)
                _log_part.append(body_out)

            compiled_out = "".join(_log_part)

            if log_config.assert_ is not None:
                if body_out.strip() != log_config.assert_.strip():
                    self._has_assert_fail = True

            self._out.append(compiled_out)

        if self._has_assert_fail:
            self._out.append("\nASSERTION FAIL")


class Simulation():
    def __init__(
            self,
            data: bytes,
            code: bytes,
            limit: int = 1,
            port_mapped_io: Optional[dict[int, list[list[Any]]]] = None,
            log_configs: Optional[list[LogConfig]] = None,
            cache_enabled: Optional[bool] = None,
    ):
        if cache_enabled is None:
            cache_enabled = MachineConfig.last_cache_enabled()
        self._machine = Machine(on_tick=self._on_tick, cache_enabled=cache_enabled)
        self._load(data, code, port_mapped_io or {})
        self._limit = limit
        self._logger = Logger(log_configs or [], self._machine)

    @property
    def logs(self):
        return self._logger.out

    def start(self):
        self._logger.start()
        self._machine.run()
        self._logger.finish()

    def _on_tick(self):
        self._logger.on_tick()
        if GLOBAL_NETLIST.tick_counter >= self._limit:
            self._logger.finish()
            raise RuntimeError("Simultion limit reached!")

    @property
    def machine(self) -> Machine:
        return self._machine

    def _load(self, data: bytes, code: bytes, port_mapped_io: dict[int, list[list[Any]]]):
        self._machine.data_mem.load(data)
        self._machine.code_mem.load(code)

        for device_idx, entries in port_mapped_io.items():
            if not 0 <= device_idx < len(self._machine.devices.external_devices):
                continue

            device = self._machine.devices.external_devices[device_idx]
            schedule: list[tuple[int, int]] = []
            last_tick = 0
            event_ticks: list[int] = []

            for entry in entries:
                tick_raw, value_raw = entry[0], entry[1]
                tick = int(tick_raw)
                last_tick = max(last_tick, tick)
                event_ticks.append(tick)
                value = ord(value_raw) if isinstance(value_raw, str) else int(value_raw)
                schedule.append((tick, value))

            device.set_irq_schedule(schedule)


def read_file(path: Path) -> bytes:
    with open(path, "rb") as file_obj:
        return file_obj.read()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Machine')
    parser.add_argument('-c', '--code', type=Path, required=True, help='Path to code memory dump')
    parser.add_argument('-d', '--data', type=Path, required=True, help='Path to data memory dump')
    parser.add_argument('-s', '--settings', type=Path, required=True, help='Path to settings file')
    args = parser.parse_args()

    with open(args.settings, 'r') as f:
        config_dict = yaml.safe_load(f)
    machine_config = MachineConfig.from_dict(config_dict)

    sim_obj = Simulation(
        read_file(args.data),
        read_file(args.code),
        machine_config.limit,
        machine_config.port_mapped_io,
        machine_config.log_configs
    )

    try:
        sim_obj.start()
    except Exception as e:
        print(e, e.with_traceback)
    finally:
        print(sim_obj.logs)
