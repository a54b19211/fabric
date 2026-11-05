"""clean_text.v12 WHO DON (T-26): title heading, HTML blocks, <table> as a markdown table; nothing dropped."""

import json

from processing import clean_text


def who(**sections):
    return json.dumps({"Title": "Mpox - Democratic Republic of the Congo", **sections})


def test_the_version_is_v12():
    assert clean_text.PROCESSOR_VERSION == "datafabric.clean_text.v12"


def test_the_title_is_a_level_one_heading_and_points_at_the_raw_title():
    out = clean_text.clean_who(who(Summary="<p>Short.</p>"))
    assert out["text"].startswith("# Mpox - Democratic Republic of the Congo\n\nShort.")
    assert out["segments"][0] == {"start": 0, "end": 41, "kind": "heading",
                                  "source": {"json_pointer": "/Title", "within": "raw", "block": 0}}


def test_an_html_table_becomes_a_markdown_table_with_its_header_and_caption():
    html = ("<p>Cases by province.</p><table><caption>Table 1. Cases</caption>"
            "<thead><tr><th>Province</th><th>Suspected</th><th>Confirmed</th></tr></thead>"
            "<tbody><tr><td>Équateur</td><td>1 444</td><td>12</td></tr>"
            "<tr><td>Sud-Kivu</td><td>380</td><td>87</td></tr></tbody></table><p>After.</p>")
    out = clean_text.clean_who(who(Overview=html))
    assert ("Cases by province.\n\nTable 1. Cases\n\n"
            "| Province | Suspected | Confirmed |\n| --- | --- | --- |\n"
            "| Équateur | 1 444 | 12 |\n| Sud-Kivu | 380 | 87 |\n\nAfter.") in out["text"]
    kinds = [(s["kind"], s["source"]["json_pointer"], s["source"]["block"]) for s in out["segments"]]
    assert kinds[1:] == [("paragraph", "/Overview", 0), ("paragraph", "/Overview", 1), ("table", "/Overview", 2),
                         ("paragraph", "/Overview", 3)]
    for s in out["segments"]:
        assert out["text"][s["start"] : s["end"]].strip()


def test_spanning_cells_keep_the_column_count_and_pipes_are_escaped():
    html = ("<table><tr><th colspan='2'>Region</th><th>n</th></tr>"
            "<tr><td>A|B</td><td>x</td><td>3</td></tr><tr><td>short</td></tr></table>")
    table = clean_text.clean_who(who(Epidemiology=html))["text"].split("\n\n")[1]
    assert table.split("\n") == ["| Region |  | n |", "| --- | --- | --- |", "| A\\|B | x | 3 |",
                                 "| short |  |  |"]


def test_headings_lists_and_loose_text_are_kept_in_order():
    html = "Loose text <b>bold</b>.<h3>Next</h3><ul><li>one</li><li>two <i>it</i></li></ul><div><p>in div</p></div>"
    text = clean_text.clean_who(who(Response=html))["text"]
    assert text.split("\n\n")[1:] == ["Loose text bold .", "### Next", "- one", "- two it", "in div"]


def test_sections_follow_the_projection_and_missing_ones_are_not_invented():
    out = clean_text.clean_who(who(Advice="<p>a</p>", Summary="<p>s</p>", FurtherInformation="<p>links</p>"))
    assert out["text"] == "# Mpox - Democratic Republic of the Congo\n\ns\n\na"  # projection order, same sections
    assert clean_text.clean_who(json.dumps({"Title": "T"}))["warnings"] == ["no_body"]
    assert "no_title" in clean_text.clean_who(json.dumps({"Summary": "<p>x</p>"}))["warnings"]
    assert "section_not_text:Overview" in clean_text.clean_who(who(Overview=["x"]))["warnings"]


def test_every_word_of_the_flattened_projection_is_still_there():
    """What the projection (get_text) had is not lost: the same words, in the same order, with structure."""
    from bs4 import BeautifulSoup

    html = ("<p>Since 1 January, <strong>1 444</strong> suspected cases.</p><table><tr><th>Week</th><th>n</th></tr>"
            "<tr><td>W1</td><td>10</td></tr></table><ul><li>Item</li></ul>")
    markup = ("|", "---", "-", "#")
    title = "Mpox - Democratic Republic of the Congo".split()
    flat = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split()
    words = [w for w in clean_text.clean_who(who(Overview=html))["text"].split() if w not in markup]
    assert words == [w for w in title + flat if w not in markup]
