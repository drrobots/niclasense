"""The tile declarations, which three renderers read and none of them validate.

plot.py, view.py and the browser client each walk TILES and PLACEMENT and trust what they
find. A tile naming a column that does not exist raises in one of them, silently draws
nothing in another, and disappears from the third; a placement that overlaps the capture
slot draws one widget on top of another. None of that is caught until something is looked
at, and the declarations are the sort of thing that gets edited by copying the tile above
it.

Also checked here: min_span. CLAUDE.md asks for one on every new tile, because without it
a resting board autoscales to its own quantization noise and reads as dramatic staircases.
That instruction is exactly the kind a test should be enforcing rather than repeating.
"""

import unittest

import support  # noqa: F401 -- puts python/ on the path
import tiles
from columns import COLUMNS

GRID_COLUMNS = 12


class Declarations(unittest.TestCase):
    def test_every_series_names_a_real_column(self):
        for tile in tiles.TILES:
            for column, _label, _colour in tile["series"]:
                self.assertIn(column, COLUMNS, "%s: %s" % (tile["name"], column))

    def test_every_tile_has_the_keys_the_renderers_read(self):
        for tile in tiles.TILES:
            for key in ("name", "title", "series", "unit", "fmt", "min_span"):
                self.assertIn(key, tile, tile.get("name"))
            self.assertTrue(tile["series"], "%s has no series" % tile["name"])

    def test_min_span_is_present_and_positive(self):
        for tile in tiles.TILES:
            self.assertGreater(tile["min_span"], 0, tile["name"])

    def test_the_format_string_accepts_a_float(self):
        """Every readout goes through `tile["fmt"] % value`; a stray %d or %s is a
        TypeError at the first sample rather than at start-up."""
        for tile in tiles.TILES:
            self.assertIsInstance(tile["fmt"] % 1.5, str, tile["name"])

    def test_a_value_tile_names_one_of_its_own_series(self):
        for tile in tiles.TILES:
            if tile.get("value"):
                columns = [column for column, _l, _c in tile["series"]]
                self.assertIn(tile["value"], columns, tile["name"])

    def test_tile_names_are_unique(self):
        names = [tile["name"] for tile in tiles.TILES]
        self.assertEqual(len(set(names)), len(names))

    def test_the_quaternion_tile_has_the_columns_it_annotates(self):
        for tile in tiles.TILES:
            if tile.get("quaternion"):
                for column in ("qx", "qy", "qz", "qw"):
                    self.assertIn(column, COLUMNS)


class Layout(unittest.TestCase):
    def cells(self, placement):
        row, column, span = placement
        return set((row, column + i) for i in range(span))

    def test_every_tile_is_placed(self):
        self.assertEqual(
            set(tiles.PLACEMENT), set(tile["name"] for tile in tiles.TILES)
        )

    def test_nothing_overlaps_including_the_capture_slot(self):
        taken = {}
        for name, placement in list(tiles.PLACEMENT.items()) + [
            ("<capture>", tiles.CAPTURE_SLOT)
        ]:
            for cell in self.cells(placement):
                self.assertNotIn(
                    cell, taken, "%s overlaps %s at %s" % (name, taken.get(cell), cell)
                )
                taken[cell] = name

    def test_nothing_runs_off_the_grid(self):
        for name, (row, column, span) in tiles.PLACEMENT.items():
            self.assertGreaterEqual(column, 0, name)
            self.assertGreater(span, 0, name)
            self.assertLessEqual(column + span, GRID_COLUMNS, name)


class Palette(unittest.TestCase):
    def test_light_overrides_are_keyed_by_colours_that_are_actually_used(self):
        """An override for a colour no tile uses is dead, and reads as a theme bug when
        the trace it was meant to fix is still unreadable."""
        used = set()
        for tile in tiles.TILES:
            for _column, _label, colour in tile["series"]:
                used.add(colour)
        for colour in tiles.LIGHT_OVERRIDES:
            self.assertIn(colour, used, colour)

    def test_both_palettes_define_the_same_roles(self):
        self.assertEqual(set(tiles.LIGHT_PALETTE), set(tiles.DARK_PALETTE))

    def test_colours_are_hex(self):
        import re

        pattern = re.compile(r"^#[0-9a-fA-F]{6}$")
        values = list(tiles.LIGHT_PALETTE.values()) + list(tiles.DARK_PALETTE.values())
        values += list(tiles.LIGHT_OVERRIDES.values())
        for tile in tiles.TILES:
            values += [colour for _c, _l, colour in tile["series"]]
        for colour in values:
            self.assertTrue(pattern.match(colour), colour)

    def test_the_bsec_tiles_exist_and_every_accuracy_word_is_covered(self):
        names = set(tile["name"] for tile in tiles.TILES)
        self.assertTrue(tiles.BSEC_TILES)
        self.assertEqual(tiles.BSEC_TILES - names, set())
        # BSEC reports 0..3; a missing entry would leave a warming-up sensor's fixed
        # placeholder looking like a live flat trace.
        self.assertEqual(set(tiles.BSEC_ACCURACY_NOTES), set((0, 1, 2, 3)))


class Bounds(unittest.TestCase):
    def test_the_window_bounds_are_ordered_and_sane(self):
        self.assertGreater(tiles.MIN_WINDOW_S, 0)
        self.assertGreater(tiles.MAX_WINDOW_S, tiles.MIN_WINDOW_S)
        self.assertGreater(tiles.MAX_POINTS, 0)


class NoRenderingDependency(unittest.TestCase):
    """Declaration only: no matplotlib, no browser, no drawing of any kind.

    This is the property that lets webhub.py serve the whole module to a browser as JSON
    while view.py draws the same declarations into a matplotlib figure. It was worth a
    test even when plot.py re-exported these constants; now that view.py imports them
    from here directly, it is the only thing keeping a stray `import matplotlib` in
    tiles.py from putting the plotting layer inside the capture's web server.
    """

    def test_importing_the_declarations_pulls_in_no_renderer(self):
        import subprocess
        import sys

        code = (
            "import sys; import tiles; "
            "print(any(m == 'matplotlib' or m.startswith('matplotlib.') for m in sys.modules))"
        )
        out = subprocess.check_output(
            [sys.executable, "-c", code], cwd=support.PYTHON_DIR
        )
        self.assertEqual(out.strip(), b"False")

    def test_view_takes_its_constants_from_here(self):
        """It used to get them from plot.py, which dragged matplotlib's plotting layer in
        to fetch a colour. plot.py is gone; this is where they live."""
        import matplotlib

        matplotlib.use("Agg")
        import view

        for name in (
            "ACCENT", "BSEC_ACCURACY_NOTES", "BSEC_TILES", "CAPTURE_SLOT", "FONT",
            "GRID", "MUTED", "PAGE_BG", "PLACEMENT", "TEXT", "TILE_BG", "TILE_EDGE",
            "TILES",
        ):
            self.assertIs(getattr(view, name), getattr(tiles, name), name)


if __name__ == "__main__":
    unittest.main()
