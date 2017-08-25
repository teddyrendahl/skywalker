#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from os import path
from math import sin, cos, pi

from pswalker.config import homs_system

from pydm import Display
from pydm.PyQt.QtCore import pyqtSlot, QCoreApplication, QPoint
from pydm.PyQt.QtGui import QDoubleValidator

logging.basicConfig(level=logging.DEBUG,
                    format=('%(asctime)s '
                            '%(name)-12s '
                            '%(levelname)-8s '
                            '%(message)s'),
                    datefmt='%m-%d %H:%M:%S',
                    filename='./skywalker_debug.log',
                    filemode='a')
logger = logging.getLogger(__name__)
MAX_MIRRORS = 4

config = homs_system()


class SkywalkerGui(Display):
    # System mapping of associated devices
    system = dict(
        m1h=dict(mirror=config['m1h'],
                 imager=config['hx2'],
                 slits=config['hx2_slits'],
                 rotation=90),
        m2h=dict(mirror=config['m2h'],
                 imager=config['dg3'],
                 slits=config['dg3_slits'],
                 rotation=90),
        mfx=dict(mirror=config['xrtm2'],
                 imager=config['mfxdg1'],
                 slits=config['mfxdg1_slits'],
                 rotation=90)
    )

    # Alignment mapping of which sets to use for each alignment
    alignments = {'HOMS': [['m1h', 'm2h']],
                  'MFX': [['mfx']],
                  'HOMS + MFX': [['m1h', 'm2h'], ['mfx']]}

    def __init__(self, parent=None, args=None):
        super().__init__(parent=parent, args=args)

        self.goal_cache = {}
        self.beam_x_stats = None
        self.imager = None

        # Populate image title combo box
        self.ui.image_title_combo.clear()
        self.all_imager_names = [entry['imager'].name for
                                 entry in self.system.values()]
        for imager_name in self.all_imager_names:
            self.ui.image_title_combo.addItem(imager_name)

        # Populate procedures combo box
        self.ui.procedure_combo.clear()
        for align in self.alignments.keys():
            self.ui.procedure_combo.addItem(align)

        # Do not connect any PVs during init. PYDM connects all needed PVs
        # right after init, so if we also connect them then we've done it
        # twice! This can cause problems with waveform cpu usage and display
        # glitches.

        # Initialize the screen with whatever the first procedure is
        self.select_procedure(self.ui.procedure_combo.currentText(),
                              connect=False)

        # Initialize the screen with the first camera in the first procedure
        system_key = self.alignments[self.procedure][0][0]
        self.select_system_entry(system_key, connect=False)

        # When we change the procedure, reinitialize the control portions
        procedure_changed = self.ui.procedure_combo.activated[str]
        procedure_changed.connect(self.on_procedure_combo_changed)

        # When we change the active imager, swap just the imager
        imager_changed = self.ui.image_title_combo.activated[str]
        imager_changed.connect(self.on_image_combo_changed)

        # When we change the goals, update the deltas
        for goal_value in self.get_widget_set('goal_value'):
            goal_changed = goal_value.editingFinished
            goal_changed.connect(self.on_goal_changed)

        self.setup_gui_logger()

    def setup_gui_logger(self):
        """
        Initializes the text stream at the bottom of the gui. This text stream
        is actually just the log messages from Python!
        """
        console = GuiHandler(self.ui.log_text)
        console.setLevel(logging.INFO)
        formatter = logging.Formatter(fmt='%(asctime)s %(message)s',
                                      datefmt='%m-%d %H:%M:%S')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)
        logger.info("Skywalker GUI initialized.")

    @pyqtSlot(str)
    def on_image_combo_changed(self, imager_name):
        self.select_imager(imager_name)

    @pyqtSlot(str)
    def on_procedure_combo_changed(self, procedure_name):
        self.select_procedure(procedure_name)

    @pyqtSlot()
    def on_goal_changed(self):
        self.update_beam_delta()

    @pyqtSlot()
    def on_start_button(self):
        pass

    @pyqtSlot()
    def on_pause_button(self):
        pass

    @pyqtSlot()
    def on_abort_button(self):
        pass

    @pyqtSlot()
    def on_slits_button(self):
        pass

    @property
    def active_system(self):
        active_system = []
        for part in self.alignments[self.procedure]:
            active_system.extend(part)
        return active_system

    @property
    def mirrors(self):
        return [self.system[act]['mirror'] for act in self.active_system]

    @property
    def imagers(self):
        return [self.system[act]['imager'] for act in self.active_system]

    @property
    def slits(self):
        return [self.system[act].get('slits') for act in self.active_system]

    @property
    def goals(self):
        vals = []
        for line_edit in self.get_widget_set('goal_value'):
            goal = line_edit.text()
            try:
                goal = float(goal)
            except:
                goal = None
            vals.append(goal)
        return vals

    def none_pad(self, obj_list):
        padded = []
        padded.extend(obj_list)
        while len(padded) < MAX_MIRRORS:
            padded.append(None)
        return padded

    @property
    def mirrors_padded(self):
        return self.none_pad(self.mirrors)

    @property
    def imagers_padded(self):
        return self.none_pad(self.imagers)

    @property
    def slits_padded(self):
        return self.none_pad(self.slits)

    @pyqtSlot(str)
    def select_procedure(self, procedure, connect=True):
        """
        Change on-screen labels and pv connections to match the current
        procedure.
        """
        logger.info('Selecting procedure %s', procedure)
        # Set the procedure member that will be used elsewhere
        self.procedure = procedure

        # Set text, pvs in the Goals and Mirrors areas
        goal_labels = self.get_widget_set('goal_name')
        goal_line_edits = self.get_widget_set('goal_value')
        slit_checkboxes = self.get_widget_set('slit_check')
        mirror_labels = self.get_widget_set('mirror_name')
        mirror_circles = self.get_widget_set('mirror_circle')
        mirror_rbvs = self.get_widget_set('mirror_readback')
        mirror_sets = self.get_widget_set('mirror_setpos')

        my_zip = zip(self.mirrors_padded,
                     self.imagers_padded,
                     self.slits_padded,
                     goal_labels,
                     goal_line_edits,
                     slit_checkboxes,
                     mirror_labels,
                     mirror_circles,
                     mirror_rbvs,
                     mirror_sets)
        for (mirr, img, slit, glabel, gedit, scheck,
             mlabel, mcircle, mrbv, mset) in my_zip:
            # Cache goal values and clear
            old_goal = str(gedit.text())
            if len(old_goal) > 0:
                self.goal_cache[str(glabel.text())] = float(old_goal)
            gedit.clear()

            # Reset all checkboxes and kill pv connections
            scheck.setChecked(False)
            if connect:
                clear_pydm_connection(mcircle)
                clear_pydm_connection(mrbv)
                clear_pydm_connection(mset)

            # If no imager, we hide the unneeded widgets
            if img is None:
                glabel.hide()
                gedit.hide()
                scheck.hide()
                mlabel.hide()
                mcircle.hide()
                mrbv.hide()
                mset.hide()

            # Otherwise, make sure the widgets are visible and set parameters
            else:
                # Basic labels for goals, mirrors, and slits
                glabel.clear()
                glabel.setText(img.name)
                mlabel.setText(mirr.name)
                if slit is None:
                    scheck.hide()
                else:
                    scheck.setText(slit.name)
                    scheck.show()

                # Set up input validation and check cache for value
                # TODO different range for different imager
                gedit.setValidator(QDoubleValidator(0., 1000., 3))
                cached_goal = self.goal_cache.get(img.name)
                if cached_goal is None:
                    gedit.clear()
                else:
                    gedit.setText(str(cached_goal))

                # Connect mirror PVs
                mcircle.channel = 'ca://' + mirr.pitch.motor_done_move.pvname
                # mrbv.channel = 'ca://' + mirr.pitch.user_readback.pvname
                mrbv.setChannel('ca://' + mirr.pitch.user_readback.pvname)
                mset.channel = 'ca://' + mirr.pitch.user_setpoint.pvname

                if connect:
                    create_pydm_connection(mcircle)
                    create_pydm_connection(mrbv)
                    create_pydm_connection(mset)

                # Make sure things are visible
                glabel.show()
                gedit.show()
                mlabel.show()
                mcircle.show()
                mrbv.show()
                mset.show()
        # If we already had set up an imager, update beam delta with new goals
        if self.imager is not None:
            self.update_beam_delta()

    def get_widget_set(self, name, num=MAX_MIRRORS):
        widgets = []
        for n in range(1, num + 1):
            widget = getattr(self.ui, name + "_" + str(n))
            widgets.append(widget)
        return widgets

    @pyqtSlot(str)
    def select_imager(self, imager_name):
        logger.info('Selecting imager %s', imager_name)
        for k, v in self.system.items():
            if imager_name == v['imager'].name:
                return self.select_system_entry(k)

    @pyqtSlot(str)
    def select_system_entry(self, system_key, connect=True):
        """
        Change on-screen information and displayed image to correspond to the
        selected mirror-imager-slit trio.
        """
        system_entry = self.system[system_key]
        imager = system_entry['imager']
        slits = system_entry.get('slits')
        rotation = system_entry.get('rotation', 0)
        self.imager = imager
        self.slit = slits
        self.rotation = rotation
        try:
            self.procedure_index = self.imagers.index(imager)
        except ValueError:
            # This means we picked an imager not in this procedure
            # This is allowed, but it means there is no goal delta!
            self.procedure_index = None

        # Make sure the combobox matches the image
        index = self.all_imager_names.index(imager.name)
        self.ui.image_title_combo.setCurrentIndex(index)
        self.ui.readback_imager_title.setText(imager.name)

        # Some cleanup
        if self.beam_x_stats is not None:
            self.beam_x_stats.clear_sub(self.update_beam_pos)

        # Set up the imager
        self.initialize_image(imager, connect=connect)

        # Centroid stuff
        stats2 = imager.detector.stats2
        self.beam_x_stats = stats2.centroid.x
        self.beam_y_stats = stats2.centroid.y

        self.beam_x_stats.subscribe(self.update_beam_pos)

        # Slits stuff
        self.ui.readback_slits_title.clear()
        slit_x_widget = self.ui.slit_x_width
        slit_y_widget = self.ui.slit_y_width
        if connect:
            clear_pydm_connection(slit_x_widget)
            clear_pydm_connection(slit_y_widget)
        if slits is not None:
            slit_x_name = slits.xwidth.readback.pvname
            slit_y_name = slits.ywidth.readback.pvname
            self.ui.readback_slits_title.setText(slits.name)
            # slit_x_widget.channel = 'ca://' + slit_x_name
            slit_x_widget.setChannel('ca://' + slit_x_name)
            # slit_y_widget.channel = 'ca://' + slit_y_name
            slit_y_widget.setChannel('ca://' + slit_y_name)
            if connect:
                create_pydm_connection(slit_x_widget)
                create_pydm_connection(slit_y_widget)

    def initialize_image(self, imager, connect=True):
        # Disconnect image PVs
        if connect:
            clear_pydm_connection(self.ui.image)
        self.ui.image.resetImageChannel()
        self.ui.image.resetWidthChannel()

        # Handle rotation
        self.ui.image.getImageItem().setRotation(self.rotation)
        size_x = imager.detector.cam.array_size.array_size_x.value
        size_y = imager.detector.cam.array_size.array_size_y.value
        pix_x, pix_y = rotate(size_x, size_y, self.rotation)
        self.pix_x = int(round(abs(pix_x)))
        self.pix_y = int(round(abs(pix_y)))

        # Connect image PVs
        image2 = imager.detector.image2
        self.ui.image.setWidthChannel('ca://' + image2.width.pvname)
        self.ui.image.setImageChannel('ca://' + image2.array_data.pvname)
        if connect:
            create_pydm_connection(self.ui.image)

        # TODO figure out how image sizing really works
        self.ui.image.resize(self.pix_x, self.pix_y)

    @pyqtSlot()
    def update_beam_pos(self, *args, **kwargs):
        centroid_x = self.beam_x_stats.value
        centroid_y = self.beam_y_stats.value

        rotation = -self.rotation
        xpos, ypos = rotate(centroid_x, centroid_y, rotation)

        if xpos < 0:
            xpos += self.pix_x
        if ypos < 0:
            ypos += self.pix_y

        self.xpos = xpos
        self.ypos = ypos

        self.ui.beam_x_value.setText(str(xpos))
        self.ui.beam_y_value.setText(str(ypos))

        self.update_beam_delta()

    @pyqtSlot()
    def update_beam_delta(self, *args, **kwargs):
        if self.procedure_index is None:
            self.ui.beam_x_delta.clear()
        else:
            goal = self.goals[self.procedure_index]
            if goal is None:
                self.ui.beam_x_delta.clear()
            else:
                self.ui.beam_x_delta.setText(str(self.xpos - goal))
        # No y delta yet, there isn't a y goal pos!
        self.ui.beam_y_delta.clear()

    def ui_filename(self):
        return 'skywalker_gui.ui'

    def ui_filepath(self):
        return path.join(path.dirname(path.realpath(__file__)),
                         self.ui_filename())


class GuiHandler(logging.Handler):
    """
    Logging handler that logs to a scrolling text widget.
    """
    terminator = '\n'

    def __init__(self, text_widget, level=logging.NOTSET):
        super().__init__(level=level)
        self.text_widget = text_widget

    def emit(self, record):
        try:
            msg = self.format(record)
            cursor = self.text_widget.cursorForPosition(QPoint(0, 0))
            cursor.insertText(msg + self.terminator)
        except Exception:
            self.handleError(record)


class BaseWidgetGroup:
    """
    A group of widgets that are part of a set with a single label.
    """
    def __init__(self, widgets, label=None, name=None, **kwargs):
        self.widgets = widgets
        self.label = label
        self.setup(name=name, **kwargs)

    def setup(self, name=None, **kwargs):
        if None not in (self.label, name):
            self.label.setText(name)

    def hide(self):
        for widget in self.widgets:
            widget.hide()
        if self.label is not None:
            self.label.hide()

    def show(self):
        for widget in self.widgets:
            widget.show()
        if self.label is not None:
            self.label.show()


class ValueWidgetGroup(BaseWidgetGroup):
    """
    A group of widgets that have a user-editable value field.
    """
    def __init__(self, line_edit, label, checkbox=None, name=None, cache=None,
                 validator=None):
        widgets = [line_edit]
        if checkbox is not None:
            widgets.append(checkbox)
        self.line_edit = line_edit
        self.checkbox = checkbox
        if cache is None:
            self.cache = {}
        else:
            self.cache = cache
        if validator is None:
            self.force_type = None
        else:
            if isinstance(validator, QDoubleValidator):
                self.force_type = float
            else:
                raise NotImplementedError
            self.line_edit.setValidator(validator)
        super().__init__(widgets, label=label, name=name)

    def setup(self, name=None, **kwargs):
        old_name = self.label.text()
        old_value = self.value
        if None not in (old_name, old_value):
            self.cache[old_name] = old_value
        super().setup(name=name, **kwargs)
        cache_value = self.cache.get(name)
        if cache_value is not None:
            self.value = cache_value
        if None not in (self.checkbox, name):
            self.checkbox.setText(name)
        if self.checkbox is not None:
            self.checkbox.setChecked(False)

    @property
    def value(self):
        raw = self.line_edit.text()
        if not raw:
            return None
        if self.force_type is None:
            return raw
        else:
            try:
                return self.force_type(raw)
            except:
                return None

    @value.setter
    def value(self, val):
        txt = str(val)
        self.line_edit.setText(txt)


class PydmWidgetGroup(BaseWidgetGroup):
    """
    A group of pydm widgets under a single label that may be set up and reset
    as a group.
    """
    protocol = 'ca://'

    def __init__(self, widgets, pvnames, label=None, name=None, **kwargs):
        super().__init__(self, widgets, label=label, name=name,
                         pvnames=pvnames, **kwargs)

    def setup(self, *, pvnames, name=None, **kwargs):
        super().setup(name=name, **kwargs)
        for widget, pvname in zip(self.widgets, pvnames):
            chan = self.protocol + pvname
            try:
                widget.setChannel(chan)
            except:
                widget.channel = chan

    def change_pvs(self, pvnames, name=None, **kwargs):
        self.clear_connections()
        self.setup(pvnames, name=name, **kwargs)
        self.create_connections()

    def clear_connections(self):
        for widget in self.widgets:
            clear_pydm_connection(widget)

    def create_connections(self):
        for widget in self.widgets:
            create_pydm_connection(widget)


class ObjWidgetGroup(PydmWidgetGroup):
    """
    A group of pydm widgets that get their channels from an object that can be
    stripped out and replaced to change context, provided the class is the
    same.
    """
    def __init__(self, widgets, attrs, obj, label=None, **kwargs):
        self.attrs = attrs
        pvnames = self.get_pvnames(obj)
        super().__init__(widgets, pvnames, label=label, name=obj.name,
                         **kwargs)

    def change_obj(self, obj, **kwargs):
        pvnames = self.get_pvnames(obj)
        self.change_pvs(pvnames, name=obj.name, **kwargs)

    def get_pvnames(self, obj):
        pvnames = []
        for attr in self.attrs:
            sig = self.nested_getattr(obj, attr)
            pvnames.append(sig.pvname)
        return pvnames

    def nested_getattr(self, obj, attr):
        steps = attr.split('.')
        for step in steps:
            obj = getattr(obj, step)
        return obj


class ImgObjWidget(ObjWidgetGroup):
    """
    Macros to set up the image widget channels from opyhd areadetector obj.
    Not really a group but this was convenient.
    """
    def __init__(self, img_widget, img_obj, rotation=0):
        attrs = ['detector.image2.width',
                 'detector.image2.array_data']
        super().__init__([img_widget], attrs, img_obj, rotation=rotation)

    def setup(self, *, pvnames, rotation=0, **kwargs):
        self.rotation = rotation
        img_widget = self.widgets[0]
        width_pv = pvnames[0]
        image_pv = pvnames[1]
        img_widget.getImageItem().setRotation(rotation)
        img_widget.resetImageChannel()
        img_widget.resetWidthChannel()
        img_widget.setWidthChannel(self.protocol + width_pv)
        img_widget.setImageChannel(self.protocol + image_pv)

    @property
    def size(self):
        rot_x, rot_y = rotate(self.raw_size_x, self.raw_size_y, self.rotation)
        return (int(round(abs(rot_x))), int(round(abs(rot_y))))

    @property
    def size_x(self):
        return self.size[0]

    @property
    def size_y(self):
        return self.size[1]

    @property
    def raw_size_x(self):
        return self.obj.detector.cam.array_size.array_size_x.value

    @property
    def raw_size_y(self):
        return self.obj.detector.cam.array_size.array_size_y.value


def clear_pydm_connection(widget):
    QApp = QCoreApplication.instance()
    QApp.close_widget_connections(widget)
    widget._channels = None


def create_pydm_connection(widget):
    QApp = QCoreApplication.instance()
    QApp.establish_widget_connections(widget)


def to_rad(deg):
    return deg*pi/180


def sind(deg):
    return sin(to_rad(deg))


def cosd(deg):
    return cos(to_rad(deg))


def rotate(x, y, deg):
    x2 = x * cosd(deg) - y * sind(deg)
    y2 = x * sind(deg) + y * cosd(deg)
    return (x2, y2)

intelclass = SkywalkerGui # NOQA
