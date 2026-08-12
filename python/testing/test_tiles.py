"""The tile declarations, which the renderer reads and does not validate.

The browser client walks TILES and PLACEMENT and trusts what it finds. A tile naming a
column that does not exist simply disappears; a placement that overlaps the capture slot
draws one widget on top of another. Neither is caught until something is looked at, and the
declarations are the sort of thing that gets edited by copying the tile above it. There were
three renderers when this was written -- plot.py and view.py drew the same declarations into
matplotlib -- and the checks were worth more then; they are kept because the failure mode is
silent either way.

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

    def test_nothing_overlaps(self):
        """There was a third thing in here -- CAPTURE_SLOT, a hole reserved in row 1 for
        the capture tile, which every other tile had to be packed around. The capture state
        moved to the page header, so the grid is now twelve equal cells of sensor and this
        check has one fewer special case."""
        taken = {}
        for name, placement in tiles.PLACEMENT.items():
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


class AlternativeUnits(unittest.TestCase):
    """A tile may offer a second unit the viewer can switch into. Display only.

    The rule worth protecting is the one that is not visible in the browser: whatever a tab
    is showing, the wire and the CSV stay in the unit the column is named in. A conversion
    that reached the file would make a log mean something different depending on what
    somebody happened to be looking at when it was written.
    """

    def alt_tiles(self):
        return [tile for tile in tiles.TILES if tile.get("alt_unit")]

    def test_the_declaration_is_complete(self):
        for tile in self.alt_tiles():
            alt = tile["alt_unit"]
            self.assertEqual(set(alt), set(("unit", "mul", "add", "min_span")), tile["name"])

    def test_the_multiplier_is_positive(self):
        """The client converts the two extremes of a window rather than every sample in
        it, which is only sound while the conversion cannot reorder them."""
        for tile in self.alt_tiles():
            self.assertGreater(tile["alt_unit"]["mul"], 0, tile["name"])

    def test_the_alternative_is_a_different_unit_with_its_own_floor(self):
        for tile in self.alt_tiles():
            self.assertNotEqual(tile["alt_unit"]["unit"], tile["unit"], tile["name"])
            self.assertGreater(tile["alt_unit"]["min_span"], 0, tile["name"])

    def test_the_column_keeps_the_name_of_the_unit_it_is_recorded_in(self):
        """temp_C stays temp_C. The schema is two-sided -- columns.py and the sketch must
        agree -- so a display unit must never reach the column list."""
        for tile in self.alt_tiles():
            for column, _label, _colour in tile["series"]:
                self.assertNotIn(
                    tile["alt_unit"]["unit"].replace("deg", ""), column.split("_")[-1:],
                    column,
                )

    def test_fahrenheit_converts_correctly(self):
        """Worth stating as arithmetic rather than trusting two constants to look right."""
        alt = dict((t["name"], t["alt_unit"]) for t in self.alt_tiles())["temperature"]
        for celsius, fahrenheit in ((0.0, 32.0), (100.0, 212.0), (-40.0, -40.0),
                                    (21.5, 70.7)):
            self.assertAlmostEqual(celsius * alt["mul"] + alt["add"], fahrenheit, places=6)


class Bounds(unittest.TestCase):
    def test_the_window_bounds_are_ordered_and_sane(self):
        self.assertGreater(tiles.MIN_WINDOW_S, 0)
        self.assertGreater(tiles.MAX_WINDOW_S, tiles.MIN_WINDOW_S)
        self.assertGreater(tiles.MAX_POINTS, 0)


class NoRenderingDependency(unittest.TestCase):
    """Declaration only: no renderer, no browser, no drawing of any kind.

    This used to be phrased as "no matplotlib", back when view.py drew these same
    declarations into a figure and the risk was a stray `import matplotlib` in tiles.py
    putting the plotting layer inside the capture's web server. matplotlib went with
    view.py, so that phrasing now passes for the wrong reason -- an import of something
    that is not installed cannot appear in sys.modules.

    Stated as it should have been: importing the declarations reaches nothing outside the
    standard library. That is the property the Windows build depends on, where the bundled
    interpreter carries pyserial and nothing else, and it is the one that would catch a
    renderer being reintroduced here.
    """

    def test_importing_the_declarations_pulls_in_no_renderer(self):
        support.assert_imports_only_stdlib(self, "tiles")


if __name__ == "__main__":
    unittest.main()
