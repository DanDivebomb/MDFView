DARK_INDICATOR_STYLE = """
QTreeWidget::indicator,
QTreeView::indicator,
QListView::indicator,
QCheckBox::indicator {
    width: 13px;
    height: 13px;
    border: 1px solid #c8c8c8;
    border-radius: 2px;
    background-color: #2b2b2b;
}

QTreeWidget::indicator:checked,
QTreeView::indicator:checked,
QListView::indicator:checked,
QCheckBox::indicator:checked {
    background-color: #4da3ff;
    border: 1px solid #ffffff;
}

QTreeWidget::indicator:indeterminate,
QTreeView::indicator:indeterminate,
QCheckBox::indicator:indeterminate {
    background-color: #808080;
    border: 1px solid #c8c8c8;
}
"""