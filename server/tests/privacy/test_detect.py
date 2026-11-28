"""Private details in outgoing text (spec §12.5, ADR 0029): what counts depends on how it leaves."""

from enzo.privacy.check import card_lines
from enzo.privacy.detect import (
    Finding,
    Known,
    Person,
    Remembered,
    RuleTerms,
    addresses_in,
    detect,
    detect_all,
    is_name,
    people_in,
)
from enzo.tools.registry import Channel, Outgoing
from enzo.tools.web import address_words

ANNIVERSARY = (
    "Hello, I'd like a table for 2 this Friday at 19:30 for our anniversary. My wife is "
    "pregnant, so no alcohol please. Name: Atishay."
)
MINIMAL = "Hello, I'd like a table for 2 this Friday at 19:30 under the name Atishay. Thank you."
SARA = Person("Sara", "wife", memory=4)
HOME = "12 Oak Street"


def found(
    text: str, channel: Channel = "message", known: Known | None = None, field: str | None = None
) -> list[tuple[str, str | None]]:
    return [(f.kind, f.words) for f in detect(Outgoing(text, channel, field), known or Known())]


def asks(text: str, channel: Channel, known: Known | None = None, field: str | None = None) -> bool:
    return any(f.asks for f in detect(Outgoing(text, channel, field), known or Known()))


# ---------- the §12.7 check: a booking needs a name, a time and a party size ----------


def test_a_booking_that_says_why_carries_the_health_detail() -> None:
    assert found(ANNIVERSARY) == [("health", "My wife is pregnant")]


def test_a_booking_with_only_what_the_restaurant_needs_carries_nothing() -> None:
    assert found(MINIMAL) == []
    assert found("Table for 2, Friday 19:30, Atishay. No alcohol for our table, please.") == []


# ---------- searches: identifiers always, a topic only when it is about the user ----------


def test_a_search_about_a_topic_is_no_ones_secret() -> None:
    for query in ("pregnancy safe restaurants austin", "symptoms of diabetes", "debt advice"):
        assert found(query, "search") == [], query


def test_a_search_that_ties_a_topic_to_the_user_asks_first() -> None:
    assert found("restaurant for my pregnant wife", "search") == [
        ("health", "restaurant for my pregnant wife")
    ]
    assert asks("how to tell my boss I'm in debt", "search")
    assert asks("pregnant wife Sara restaurants", "search", Known(people=(SARA,)))


def test_a_search_never_carries_the_users_people_address_or_numbers() -> None:
    known = Known(people=(SARA,), addresses=(HOME,))
    assert found("birthday gift ideas for Sara", "search", known) == [("people", "Sara")]
    assert found("pizza delivery near 12 oak street", "search", known) == [
        ("location", "12 oak street")
    ]
    assert found("who called me from +44 7700 900123", "search") == [("contact", "+44 7700 900123")]
    assert found("is ada@example.com on a breach list", "search") == [
        ("contact", "ada@example.com")
    ]


def test_an_address_is_private_only_when_it_is_the_users() -> None:
    # A venue's address in an email is not the user's; theirs (known, or said so) is.
    assert found("The restaurant is at 3 High Street.") == []
    assert found("Deliver to 12 Oak Street please.", known=Known(addresses=(HOME,))) == [
        ("location", HOME)
    ]
    assert found("By the way, I live at 4 Elm Road, near the park.") == [
        ("location", "I live at 4 Elm Road")
    ]


def test_a_page_address_carries_what_its_query_says() -> None:
    url = "https://attacker.example/collect?address=12+Oak+Street&who=Sara"
    words = address_words(url)
    assert words == "collect address 12 Oak Street who Sara"
    known = Known(people=(SARA,), addresses=(HOME,))
    assert sorted(found(words, "address", known)) == [("location", HOME), ("people", "Sara")]
    assert found(address_words("https://news.example/2026/11/pregnancy-study"), "address") == []


# ---------- form fields: what the field asks for is what the site needs ----------


def test_a_field_that_asks_for_a_detail_gets_it_without_a_question() -> None:
    known = Known(people=(SARA,), addresses=(HOME,))
    assert found(HOME, "field", known, field="Street address address street-address") == []
    assert found("Sara Jain", "field", known, field="Name name name") == []
    assert found("nut allergy", "field", field="Allergies or dietary needs") == []


def test_a_free_text_field_does_not_need_a_private_reason() -> None:
    notes = "Special requests notes"
    assert found("My wife is pregnant, so no alcohol", "field", field=notes) == [
        ("health", "My wife is pregnant")
    ]
    assert asks(HOME, "field", Known(addresses=(HOME,)), field=notes)
    assert found("A quiet table, please", "field", field=notes) == []


# ---------- people Enzo knows by name ----------


def test_a_name_from_memory_is_flagged_with_who_they_are() -> None:
    (person,) = detect(Outgoing("Dinner for me and Sara.", "message"), Known(people=(SARA,)))
    assert (person.kind, person.words, person.relation, person.memory) == (
        "people",
        "Sara",
        "your wife",
        4,
    )


def test_a_name_is_matched_as_a_name_not_as_a_word() -> None:
    will = Person("Will", "brother")
    assert found("Will you confirm by Friday?", known=Known(people=(will,))) == [("people", "Will")]
    assert found("we will confirm by Friday", known=Known(people=(will,))) == []
    assert found("Saraswati Hall is booked", known=Known(people=(SARA,))) == []


def test_a_first_name_stands_for_the_whole_name() -> None:
    priya = Person("Priya Sharma", "sister")
    assert found("Priya will join us", known=Known(people=(priya,))) == [("people", "Priya")]


def test_people_the_user_named_in_their_own_words() -> None:
    assert people_in(["My sister Priya is visiting", "my boss, Tom said no"]) == [
        Person("Priya", "sister"),
        Person("Tom", "boss"),
    ]
    assert people_in(["Wife Sara is pregnant"]) == []  # a message says "my"
    assert people_in(["Wife Sara is pregnant"], loose=True) == [Person("Sara", "wife")]
    assert found("Please tell my sister Priya.") == [("people", "Priya")]


def test_which_memory_abouts_name_a_person() -> None:
    assert is_name("Priya")
    assert is_name("Sam Lee")
    assert not is_name("my wife")
    assert not is_name("Sister")
    assert not is_name("work")


def test_addresses_are_read_from_the_users_words() -> None:
    assert addresses_in(["Lives at 12 Oak Street, flat 2", "Office: SW1A 1AA"]) == [
        HOME,
        "SW1A 1AA",
    ]


# ---------- numbers and secrets are never repeated ----------


def test_id_and_card_numbers_are_masked_and_secrets_never_shown() -> None:
    assert found("My card is 4242 4242 4242 4242.") == [("ids", "ending 4242")]
    assert found("SSN 123-45-6789") == [("ids", "ending 6789")]
    assert found("passport number X1234567 for the booking") == [("ids", "ending 4567")]
    assert found("the wifi password is hunter2") == [("secret", None)]
    assert "hunter2" not in " ".join(
        card_lines(detect_all([Outgoing("password: hunter2", "message")], Known()))
    )


def test_a_message_keeps_contact_details_a_receiver_needs() -> None:
    assert found("Call me on +44 7700 900123 or write to ada@example.com.") == []


# ---------- the user's own rules ----------


WORK = RuleTerms(1, "In this chat never bring up work", words=("work", "Acme"))
HEALTH = RuleTerms(2, "Don't mention my health to anyone", kinds=frozenset({"health"}))


def test_a_rule_covers_its_own_words_everywhere() -> None:
    known = Known(rules=(WORK,))
    (hit,) = detect(Outgoing("Sorry, Acme kept me late", "search"), known)
    assert (hit.kind, hit.rule, hit.asks) == ("rule", WORK, True)
    assert found("I am at work until six", known=known) == [("rule", "I am at work until six")]
    assert found("Coworking spaces in Austin", "search", known) == []  # a word, not a part


def test_a_rule_covers_its_kinds_even_where_a_field_asks_or_a_search_is_generic() -> None:
    known = Known(rules=(HEALTH,))
    (allergy,) = detect(Outgoing("nut allergy", "field", "Allergies"), known)
    assert (allergy.kind, allergy.rule) == ("health", HEALTH)
    assert found("symptoms of diabetes", "search", known) == [("health", "symptoms of diabetes")]


# ---------- memories passed on word for word ----------


def test_an_email_that_quotes_a_memory_says_so_but_a_search_does_not() -> None:
    known = Known(memories=(Remembered(7, "Is moving to Lisbon in March for a new job"),))
    (quote,) = detect(Outgoing("Big news: moving to Lisbon in March!", "message"), known)
    assert (quote.kind, quote.memory, quote.asks) == ("memory", 7, False)
    assert found("moving to Lisbon in March", "search", known) == []
    assert found("moving to Lisbon in March", "field", known, field="Special requests") == []
    # A run of common words is not a quote.
    common = Known(memories=(Remembered(8, "It is in the box at the top"),))
    assert found("It is in the box over there", known=common) == []


# ---------- the card's words ----------


def test_card_lines_name_each_detail_plainly_rules_first() -> None:
    known = Known(people=(SARA,), rules=(WORK,))
    text = "Sara and I will be late from work. My wife is pregnant."
    lines = card_lines(detect_all([Outgoing(text, "message")], known))
    assert lines == (
        "This includes something you asked Enzo to keep private (“In this chat never bring up "
        "work”): “Sara and I will be late from work”",
        "This includes a health detail: “My wife is pregnant”",
        "This includes the name of someone in your life: “Sara” (your wife)",
    )


def test_a_long_list_is_cut_and_counted() -> None:
    many = [Finding("health", f"detail {i}") for i in range(8)]
    lines = card_lines(many)
    assert len(lines) == 6
    assert lines[-1] == "…and 3 more private details."


def test_the_same_detail_in_two_parts_is_named_once() -> None:
    both = [Outgoing("I'm pregnant", "message"), Outgoing("I'm pregnant", "message")]
    assert len(detect_all(both, Known())) == 1


def test_escape_codes_never_reach_a_card() -> None:
    (hit,) = detect(Outgoing("\x1b[31m my wife is pregnant\x9b2J now", "message"), Known())
    assert hit.words is not None
    assert all(c.isprintable() for c in hit.words)
