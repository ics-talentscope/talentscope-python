import re


_DATA_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DELETE\s+FROM|DELETE\s+[A-Z_][\w$#]*\s+FROM|TRUNCATE(?:\s+TABLE)?|"
    r"DROP\s+(?:TABLE|COLUMN|DATABASE|SCHEMA))\b",
    re.IGNORECASE,
)


def assert_data_preserving_migration(filename: str, sql: str) -> None:
    without_comments = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    without_comments = re.sub(r"(?m)^\s*--.*$", "", without_comments)
    match = _DATA_DESTRUCTIVE_SQL.search(without_comments)
    if match:
        raise RuntimeError(
            f"Migration {filename} contains data-destructive SQL; "
            "preserve existing rows and use additive or conditional changes."
        )
