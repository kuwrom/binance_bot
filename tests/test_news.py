from shortbot.news import bearish_keywords, mentions, normalize_base


def test_no_false_positives_on_common_words_and_letters():
    assert not mentions("U.S. SEC sues exchange", "S")
    assert not mentions("AT&T earnings beat", "T")
    assert not mentions("Trump says tariffs are coming", "TRUMP")
    assert not mentions("the people want lower fees", "PEOPLE")
    assert not mentions("someone bought the dip", "ONE")


def test_context_matches_for_ambiguous_tickers():
    assert mentions("$TRUMP token jumps 40%", "TRUMP")
    assert mentions("Harmony (ONE) rallies", "ONE")
    assert mentions("$S surges after upgrade", "S")


def test_multiplier_prefix_and_aliases():
    assert normalize_base("1000PEPE") == "PEPE" and normalize_base("1MBABYDOGE") == "BABYDOGE"
    assert mentions("PEPE whales accumulate", "1000PEPE")
    assert mentions("Solana network outage", "SOL")
    assert mentions("QNT Leads Altcoin Rally", "QNT")


def test_bearish_keywords_are_whole_words():
    assert bearish_keywords("Protocol hacked, funds drained") == ["drained", "hacked"]
    assert bearish_keywords("Hackathon winners and drug policy") == []
    assert "delisting" in bearish_keywords("Binance announces delisting of XYZ")
