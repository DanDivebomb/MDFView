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

/* Rollbalken. Ohne diese Regeln zeichnet Windows sie im hellen
   Systemstil - mitten im dunklen Fenster bleibt dann ein blauer
   Streifen unter jeder Kanalliste stehen. */
QScrollBar:horizontal {
    background: #2b2b2b;
    height: 12px;
    margin: 0px;
    border: none;
}

QScrollBar:vertical {
    background: #2b2b2b;
    width: 12px;
    margin: 0px;
    border: none;
}

QScrollBar::handle:horizontal {
    background: #5a5a5a;
    border-radius: 5px;
    min-width: 24px;
}

QScrollBar::handle:vertical {
    background: #5a5a5a;
    border-radius: 5px;
    min-height: 24px;
}

QScrollBar::handle:hover {
    background: #767676;
}

QScrollBar::handle:pressed {
    background: #8f8f8f;
}

QScrollBar::add-line,
QScrollBar::sub-line {
    width: 0px;
    height: 0px;
    border: none;
    background: none;
}

QScrollBar::add-page,
QScrollBar::sub-page {
    background: none;
}

QAbstractScrollArea::corner {
    background: #2b2b2b;
    border: none;
}
"""
