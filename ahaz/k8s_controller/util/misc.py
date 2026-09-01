# This exists purely because Python is a dogshit language.

def str_to_bool(s: str) -> bool:
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    raise ValueError(f"Invalid boolean string: {s}")
