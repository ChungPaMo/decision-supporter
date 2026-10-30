from export_contract_schema import OUT, build


def test_schema_file_is_up_to_date():
    assert OUT.read_text(encoding="utf-8") == build(), "계약이 바뀌었다: python export_contract_schema.py 를 실행해 contracts/ 를 갱신하세요"
