from pathlib import Path
from Config import AddressingMode, Args, Opcode, Registers, INTERRUPT_COUNT, instruction_size

DATA_WORD_SIZE = 5


def from_bytes(value: bytes) -> int:
    return int.from_bytes(value, byteorder='big') 

class Viewer:
    def __init__(self, program: bytes, data: bytes | None = None) -> None:
        self._program = program
        self._ptr = 0
        self._data_words = self._parse_data_words(data or b"")
        self._vectors_printed = False
        self._vector_table_offset = (
            instruction_size(Opcode.LD) * 2
            + instruction_size(Opcode.JMP)
        )

    @staticmethod
    def read_file(path: str) -> bytes:
        with open(path, "rb") as file_obj:
            return file_obj.read()

    @staticmethod
    def _parse_data_words(data: bytes) -> list[int]:
        if len(data) % DATA_WORD_SIZE != 0:
            return []

        return [
            from_bytes(data[offset:offset + DATA_WORD_SIZE])
            for offset in range(0, len(data), DATA_WORD_SIZE)
        ]
    
    def _read_chunk(self, size: int, context: str) -> bytes:
        next_ptr = self._ptr + size
        if next_ptr > len(self._program):
            raise ValueError(
                f"Unexpected EOF while reading {context} at offset {self._ptr:#x}"
            )

        chunk = self._program[self._ptr:next_ptr]
        self._ptr = next_ptr
        return chunk

    def _format_args(self, opcode: Opcode, args: list[int]) -> str:
        if not args:
            return "-"

        if opcode in {Opcode.JMP, Opcode.JZ, Opcode.JNZ, Opcode.CALL} and len(args) == 1:
            return f"0x{args[0]:x}"

        if opcode == Opcode.LD and len(args) == 2:
            reg_value, operand = args

            try:
                reg = Registers(reg_value).name
            except ValueError:
                reg = f"UNKNOWN({reg_value})"

            return f"reg={reg}, operand=0x{operand:x}"

        if opcode in {Opcode.PUSH, Opcode.POP} and len(args) == 2:
            mode_value, operand = args

            try:
                mode = AddressingMode(mode_value).name
            except ValueError:
                mode = f"UNKNOWN({mode_value})"

            if mode == AddressingMode.MEM.name and 0 <= operand < len(self._data_words):
                return f"mode={mode}, operand=0x{operand:x}, mem[0x{operand:x}]={self._data_words[operand]}"
            
            return f"mode={mode}, operand=0x{operand:x}"

        return ", ".join(str(hex(arg)) for arg in args)

    def __call__(self) -> None:
        while self._ptr < len(self._program):
            instruction_ptr = self._ptr

            opcode_raw = self._read_chunk(1, "opcode")
            hex_view = opcode_raw.hex()

            opcode = Opcode(from_bytes(opcode_raw))
            arg_sizes = Args[opcode.name].value

            args: list[int] = []
            for arg_idx, arg_size in enumerate(arg_sizes):
                raw_arg = self._read_chunk(arg_size, f"arg#{arg_idx} ({opcode.name})")
                hex_view += raw_arg.hex()
                args.append(from_bytes(raw_arg))

            arg_view = self._format_args(opcode, args)
            print(f"{instruction_ptr:06x} - {hex_view:<14} - {opcode.name:<5} {arg_view}")

            if not self._vectors_printed and self._ptr == self._vector_table_offset:
                self._print_interrupt_vectors()
                self._vectors_printed = True

    def _print_interrupt_vectors(self) -> None:
        for idx in range(INTERRUPT_COUNT):
            vector_ptr = self._ptr
            raw = self._read_chunk(4, f"interrupt vector #{idx}")
            target = from_bytes(raw)
            print(f"{vector_ptr:06x} - {raw.hex():<14} - VEC{idx:<2} 0x{target:x}")


if __name__ == "__main__":
    code_path = Path("/home/nf/Рабочий стол/итмо/ak_lab4/exec_code")
    data_path = Path("/home/nf/Рабочий стол/итмо/ak_lab4/exec_data")

    code = Viewer.read_file(str(code_path))
    data = Viewer.read_file(str(data_path)) if data_path.exists() else b""

    # print(code.hex())
    
    Viewer(code, data)()
