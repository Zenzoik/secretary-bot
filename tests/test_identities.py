from secretary_bot.identities import contact_label, user_label


def test_contact_label_prefers_useful_name_and_username() -> None:
    assert contact_label("Олена", "olena") == "Олена · @olena"
    assert contact_label(".", "@poldotk") == "@poldotk"


def test_contact_label_never_falls_back_to_numeric_identifier() -> None:
    assert contact_label(None, None) == "Контакт без імені"
    assert user_label(None, None) == "Користувач без імені"
