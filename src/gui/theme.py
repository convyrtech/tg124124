"""Hacker-style dark theme for Dear PyGui."""
import dearpygui.dearpygui as dpg


def create_hacker_theme() -> int:
    """Create dark green hacker theme. Returns theme ID."""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            # Background colors - dark
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (22, 27, 34))

            # Text - green
            dpg.add_theme_color(dpg.mvThemeCol_Text, (88, 166, 92))
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (68, 85, 68))

            # Borders
            dpg.add_theme_color(dpg.mvThemeCol_Border, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))

            # Frame (input fields, etc)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (33, 38, 45))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (44, 49, 56))

            # Title bar
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (22, 27, 34))

            # Buttons
            dpg.add_theme_color(dpg.mvThemeCol_Button, (35, 134, 54))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (46, 160, 67))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (29, 111, 45))

            # Headers (tables, etc)
            dpg.add_theme_color(dpg.mvThemeCol_Header, (35, 134, 54, 80))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (46, 160, 67, 100))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (29, 111, 45, 120))

            # Selection
            dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, (35, 134, 54, 100))

            # Scrollbar
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (58, 64, 71))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, (68, 74, 81))

            # Tab
            dpg.add_theme_color(dpg.mvThemeCol_Tab, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, (35, 134, 54))
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, (35, 134, 54))

            # Table
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (33, 38, 45))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (18, 22, 28))

            # Styles
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 4)

    return theme


def create_status_themes() -> dict:
    """Create themes for different status indicators."""
    themes = {}

    # Healthy - bright green
    with dpg.theme() as healthy:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (46, 204, 64))
    themes["healthy"] = healthy

    # Error - red
    with dpg.theme() as error:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 82, 82))
    themes["error"] = error

    # Pending - yellow
    with dpg.theme() as pending:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 193, 7))
    themes["pending"] = pending

    # Migrating - blue
    with dpg.theme() as migrating:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (66, 165, 245))
    themes["migrating"] = migrating

    return themes
