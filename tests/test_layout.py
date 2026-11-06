from scan_answer.template.layout import grid_cells


def test_grid_cells_horizontal_options_with_block_gap():
    cells = grid_cells((10.0, 20.0), n_groups=6, n_options=3, option_pitch=5.0, group_pitch=4.0,
                       cell_size=(2.0, 1.0), options_horizontal=True, block_every=5, block_gap=3.0)
    assert len(cells) == 6 and len(cells[0]) == 3
    assert cells[0][0] == (9.0, 19.5, 2.0, 1.0)
    assert cells[0][2] == (19.0, 19.5, 2.0, 1.0)  # 第 3 个选项右移 2*5
    assert cells[4][0][1] == 19.5 + 4 * 4.0
    assert cells[5][0][1] == 19.5 + 5 * 4.0 + 3.0  # 第 6 组多出 block_gap


def test_grid_cells_vertical_options():
    cells = grid_cells((0.0, 0.0), n_groups=2, n_options=10, option_pitch=3.0, group_pitch=5.0,
                       cell_size=(2.0, 2.0), options_horizontal=False)
    assert cells[1][9] == (4.0, 26.0, 2.0, 2.0)  # 第 2 位、数字 9：x=5，y=27
