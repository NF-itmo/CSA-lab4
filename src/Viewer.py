from pathlib import Path
try:
    from .Config import (
        AddressingMode,
        Args,
        Opcode,
        General,
        instruction_size,
    )
except ImportError:
    from Config import (
        AddressingMode,
        Args,
        Opcode,
        General,
        instruction_size,
    )
import argparse

MODE_LABELS = {
    AddressingMode.STI: "(ST)",
    AddressingMode.ST_INC: "+(ST)",
    AddressingMode.ST_DEC: "(ST)-",
    AddressingMode.MEMI: "(MEM)",
}


def from_bytes(value: bytes) -> int:
    return int.from_bytes(value, byteorder="big")


class Viewer:
    def __init__(self, program: bytes, data: bytes | None = None) -> None:
        self._program = program
        self._ptr = 0
        self._data = data or b""
        self._vectors_printed = False
        self._vector_table_offset = instruction_size(Opcode.MOV) * 2 + instruction_size(
            Opcode.JMP
        )
        self._out: str = ""

    @staticmethod
    def read_file(path: Path) -> bytes:
        with open(path, "rb") as file_obj:
            return file_obj.read()

    @staticmethod
    def _data_word_at(data: bytes, addr: int) -> int | None:
        if addr < 0 or addr + General.DATA_WORD_SIZE > len(data):
            return None
        return int.from_bytes(
            data[addr:addr + General.DATA_WORD_SIZE],
            byteorder="little",
        )

    def _read_chunk(self, size: int, context: str) -> bytes:
        next_ptr = self._ptr + size
        if next_ptr > len(self._program):
            raise ValueError(
                f"Unexpected EOF while reading {context} at offset {self._ptr:#x}"
            )

        chunk = self._program[self._ptr: next_ptr]
        self._ptr = next_ptr
        return chunk

    @staticmethod
    def _mode_name(mode_value: int) -> str:
        try:
            mode = AddressingMode(mode_value)
            return MODE_LABELS.get(mode, mode.name)
        except ValueError:
            return f"UNKNOWN({mode_value})"

    @staticmethod
    def _mode(mode_value: int) -> AddressingMode | None:
        try:
            return AddressingMode(mode_value)
        except ValueError:
            return None

    @classmethod
    def _mode_pair_names(cls, descriptor: int) -> tuple[str, str]:
        return cls._mode_name((descriptor >> 4) & 0xF), cls._mode_name(descriptor & 0xF)

    @staticmethod
    def format_args(opcode: Opcode, args: list[int], data_words: list[int] | bytes) -> str:
        if not args:
            return "-"

        if opcode == Opcode.POLY and len(args) >= 2:
            degree = args[0]
            coeffs = args[1:]
            return f"degree={degree}, coeffs={coeffs}"

        if (
            opcode in {Opcode.JMP, Opcode.JZ, Opcode.JNZ, Opcode.CALL}
        ):
            if opcode == Opcode.JMP and len(args) == 1:
                return f"0x{args[0]:x}"
            if len(args) == 2:
                return f"mode={Viewer._mode_name(args[0])}, target=0x{args[1]:x}"

        if opcode == Opcode.MOV and len(args) == 2:
            dst, src = Viewer._mode_pair_names(args[0])
            src_mode = Viewer._mode(args[0] & 0xF)
            operand = args[1]
            mem_value = Viewer._data_word_at(data_words, operand) if isinstance(data_words, bytes) else None
            if src_mode == AddressingMode.MEM and mem_value is not None:
                return f"dst={dst}, src={src}, operand=0x{operand:x}, mem[0x{operand:x}]={mem_value}"
            return f"dst={dst}, src={src}, operand=0x{operand:x}"

        if opcode in {Opcode.PLS, Opcode.MIN, Opcode.DIV, Opcode.MUL, Opcode.EQ, Opcode.GT, Opcode.LT} and len(args) == 1:
            dst, src = Viewer._mode_pair_names(args[0])
            return f"dst={dst}, src={src}"

        if opcode in {Opcode.INC, Opcode.DEC, Opcode.IN, Opcode.INT} and len(args) == 1:
            return f"mode={Viewer._mode_name(args[0])}"

        if opcode == Opcode.OUT and len(args) == 1:
            dev, value = Viewer._mode_pair_names(args[0])
            return f"device={dev}, value={value}"

        return ", ".join(str(hex(arg)) for arg in args)

    def __call__(self) -> str:
        while self._ptr < len(self._program):
            instruction_ptr = self._ptr

            opcode_raw = self._read_chunk(1, "opcode")
            hex_view = opcode_raw.hex()

            opcode = Opcode(from_bytes(opcode_raw))

            args: list[int] = []
            if opcode == Opcode.POLY:
                raw_degree = self._read_chunk(1, "POLY degree")
                hex_view += raw_degree.hex()
                degree = from_bytes(raw_degree)
                args.append(degree)

                for coeff_idx in range(degree + 1):
                    raw_coeff = self._read_chunk(3, f"POLY coeff#{coeff_idx}")
                    hex_view += raw_coeff.hex()
                    coeff_raw = from_bytes(raw_coeff)
                    if coeff_raw & 0x800000:
                        coeff_raw -= 1 << 24
                    args.append(coeff_raw)
            else:
                arg_sizes = Args[opcode.name].value
                for arg_idx, arg_size in enumerate(arg_sizes):
                    raw_arg = self._read_chunk(arg_size, f"arg#{arg_idx} ({opcode.name})")
                    hex_view += raw_arg.hex()
                    args.append(from_bytes(raw_arg))

            arg_view = self.format_args(opcode, args, self._data)
            self._out += f"{instruction_ptr:06x} - {hex_view:<{General.CODE_WORD_SIZE * 2}} - {opcode.name:<5} {arg_view}\n"

            if not self._vectors_printed and self._ptr == self._vector_table_offset:
                self._print_interrupt_vectors()
                self._vectors_printed = True

        return self._out

    def _print_interrupt_vectors(self) -> None:
        for idx in range(General.INTERRUPT_COUNT):
            vector_ptr = self._ptr
            raw = self._read_chunk(General.INTERRUT_SIZE, f"interrupt vector #{idx}")
            target = from_bytes(raw)
            self._out += f"{vector_ptr:06x} - {raw.hex():<{General.CODE_WORD_SIZE * 2}} - VEC{idx:<2} 0x{target:x}\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Binary viewer')
    parser.add_argument('-c', '--code', type=Path, required=True, help='Path to code memory dump')
    parser.add_argument('-d', '--data', type=Path, required=True, help='Path to data memory dump')
    args = parser.parse_args()

    code = Viewer.read_file(args.code)
    data = Viewer.read_file(args.data) if args.data.exists() else b""

    result = Viewer(code, data)()

    print(result)
