from datetime import datetime, timedelta

import visuals

T0 = datetime(2026, 10, 26, 11, 0)


def _frame(tmp_path, name, seconds, source="auto", window="pharmacy", url=None):
    shots = tmp_path / "screenshots"
    shots.mkdir(exist_ok=True)
    (shots / name).write_bytes(b"jpg")
    return {"staged": name, "path": tmp_path / name, "source": source, "klass": "anchored",
            "part": 1, "offset": seconds, "taken": T0 + timedelta(seconds=seconds),
            "origin": ({"app": "Ghostty", "window": window, **({"url": url} if url else {})}
                       if source == "auto" else None)}


def _fake_model(monkeypatch, replies):
    prompts = {}

    def caption_one(prompt, name, out_dir):
        prompts[name] = prompt
        return replies[name]

    monkeypatch.setattr(visuals, "_caption_one", caption_one)
    return prompts


class TestScrolledFrameKeepsItsScope:
    """26.10: the frame with '94 of 328 completed inspections' didn't show that the
    query was for 2026 only — that was on the frame before — and the summary
    turned it into an all-time figure."""

    def test_passes_the_previous_caption_from_the_same_window_so_the_period_carries_over(self, tmp_path, monkeypatch):
        recs = [_frame(tmp_path, "a.jpg", 100), _frame(tmp_path, "b.jpg", 200)]
        prompts = _fake_model(monkeypatch, {"a.jpg": "Inspekce z roku 2026: 1 122.",
                                            "b.jpg": "Pokračování dotazu za 2026: 94 z 328."})

        visuals.caption(recs, tmp_path, {})

        assert "Inspekce z roku 2026: 1 122." in prompts["b.jpg"]
        assert "100 s earlier" in prompts["b.jpg"]

    def test_does_not_mix_in_context_from_another_window(self, tmp_path, monkeypatch):
        recs = [_frame(tmp_path, "a.jpg", 100), _frame(tmp_path, "b.jpg", 200, window="Tabulky Google")]
        prompts = _fake_model(monkeypatch, {"a.jpg": "Terminál.", "b.jpg": "Tabulka."})

        visuals.caption(recs, tmp_path, {})

        assert "Terminál." not in prompts["b.jpg"]


    def test_tells_pages_apart_by_url_because_every_dub_admin_tab_has_the_same_title(self, tmp_path, monkeypatch):
        # 26.10 DUB re-run: a dub-test sign-in page was compared with a prod
        # inspection — a SAME there would have dropped the frame silently.
        recs = [_frame(tmp_path, "a.jpg", 100, window="Hřiště hrou",
                       url="https://admin.dub.hristehrou.cz/inspections/21552"),
                _frame(tmp_path, "b.jpg", 200, window="Hřiště hrou",
                       url="https://admin.dub-test.hristehrou.cz/sign_in"),
                _frame(tmp_path, "c.jpg", 300, window="Hřiště hrou",
                       url="https://admin.dub.hristehrou.cz/inspections/21552?tab=photos")]
        prompts = _fake_model(monkeypatch, {"a.jpg": "Kontrola 21552.", "b.jpg": "Přihlášení.",
                                            "c.jpg": "Fotky kontroly."})

        visuals.caption(recs, tmp_path, {})

        assert "Kontrola 21552." not in prompts["b.jpg"]
        assert "Kontrola 21552." in prompts["c.jpg"]


class TestNoiseFramesAreDropped:
    """Two identical ERR_NETWORK_CHANGED pages and eight near-identical sheet frames
    used caption slots and cluttered the visual context."""

    def test_drops_error_pages_and_repeats_and_removes_their_files_so_they_are_not_published(self, tmp_path, monkeypatch):
        recs = [_frame(tmp_path, "a.jpg", 100), _frame(tmp_path, "err.jpg", 150),
                _frame(tmp_path, "same.jpg", 200)]
        _fake_model(monkeypatch, {"a.jpg": "Tabulka.", "err.jpg": "SKIP", "same.jpg": "SAME."})

        kept = visuals.caption(recs, tmp_path, {})

        assert [r["staged"] for r in kept] == ["a.jpg"]
        assert not (tmp_path / "screenshots" / "err.jpg").exists()
        assert not (tmp_path / "screenshots" / "same.jpg").exists()

    def test_never_offers_to_drop_a_photo_or_screenshot_taken_on_purpose(self, tmp_path, monkeypatch):
        recs = [_frame(tmp_path, "p.jpg", 100, source="phone")]
        prompts = _fake_model(monkeypatch, {"p.jpg": "Vytištěný protokol."})

        kept = visuals.caption(recs, tmp_path, {})

        assert "SKIP" not in prompts["p.jpg"] and "SAME" not in prompts["p.jpg"]
        assert len(kept) == 1

    def test_keeps_an_image_whose_captioning_failed_rather_than_losing_it(self, tmp_path, monkeypatch):
        recs = [_frame(tmp_path, "a.jpg", 100)]
        _fake_model(monkeypatch, {"a.jpg": None})

        assert len(visuals.caption(recs, tmp_path, {})) == 1
