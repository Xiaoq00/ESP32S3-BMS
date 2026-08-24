import json
d = json.load(open(r"C:/Users/xiaoq/jk-bms-monitor/settings_raw.json"))
b = bytes.fromhex(d["hex"])
print("len", len(b), "hdr", b[:5].hex(), "frametype", b[4])
crc = sum(b[:299]) % 256
print("crc byte", b[-1], "calc", crc, "OK" if b[-1] == crc else "FAIL")
# 已知量锚点
caps = {
    "cap uint16 x0.1Ah=1190 (A6 04)": bytes([0xA6, 0x04]),
    "cap uint16 x1Ah=119 (77 00)": bytes([0x77, 0x00]),
    "cap uint32 x.001Ah=119000 (B8 D0 01 00)": bytes([0xB8, 0xD0, 0x01, 0x00]),
}
print("cell count 20 (0x14) offsets:", [i for i in range(len(b)) if b[i] == 20][:30])
for name, pat in caps.items():
    print(name, "-> at", b.find(pat))
print("\n=== offset  u16     u32      note ===")
for off in range(4, 140, 2):
    u16 = int.from_bytes(b[off:off + 2], "little")
    u32 = int.from_bytes(b[off:off + 4], "little") if off + 4 <= len(b) else 0
    note = ""
    if 2000 <= u16 <= 4500:
        note = f"~{u16/1000:.3f}V"
    if 100 <= u16 <= 200:
        note = f"~{u16}Ah?"
    print(f"{off:3d}   {u16:6d}  {u32:10d}   {note}")
