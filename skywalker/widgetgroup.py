#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pydm.PyQt.QtCore import QCoreApplication
from pydm.PyQt.QtGui import QDoubleValidator

from .utils import ad_stats_x_axis_rot


class BaseWidgetGroup:
    """
    A group of widgets that are part of a set with a single label.
    """
    def __init__(self, widgets, label=None, name=None, **kwargs):
        """
        Parameters
        ----------
        widgets: list
            list of widgets in the group

        label: QLabel, optional
            A special widget that acts as the label for the group

        name: str, optional
            The label text
        """
        self.widgets = widgets
        self.label = label
        self.setup(name=name, **kwargs)

    def setup(self, name=None, **kwargs):
        """
        Do basic widget setup. For Base, this is just changing the label text.
        """
        if None not in (self.label, name):
            self.label.setText(name)

    def hide(self):
        """
        Hide all widgets in group.
        """
        for widget in self.widgets:
            widget.hide()
        if self.label is not None:
            self.label.hide()

    def show(self):
        """
        Show all widgets in group.
        """
        for widget in self.widgets:
            widget.show()
        if self.label is not None:
            self.label.show()

    def text(self):
        if self.label is None:
            return None
        else:
            return self.label.text()


class ValueWidgetGroup(BaseWidgetGroup):
    """
    A group of widgets that have a user-editable value field.
    """
    def __init__(self, line_edit, label, checkbox=None, name=None, cache=None,
                 validator=None):
        """
        Parameters
        ----------
        line_edit: QLineEdit
            The user-editable value field.

        checkbox: QCheckbox, optional
            Optional checkbox widget associated with the value.

        cache: dict, optional
            For widgets that need to save/share values

        validator: QDoubleValidator, optional
            Make sure the text is a double
        """
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
        """
        Put name in the checkbox too
        """
        super().setup(name=name, **kwargs)
        if None not in (self.checkbox, name):
            self.checkbox.setText(name)
        if self.checkbox is not None:
            self.checkbox.setChecked(False)
        self.load_value(name)

    def save_value(self):
        """
        Stash current value in self.cache
        """
        old_name = self.label.text()
        old_value = self.value
        if None not in (old_name, old_value):
            self.cache[old_name] = old_value

    def load_value(self, name):
        """
        Grab current value from self.cache
        """
        cache_value = self.cache.get(name)
        if cache_value is not None:
            self.value = cache_value

    def clear(self):
        """
        Reset the value
        """
        self.line_edit.clear()

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

    @property
    def is_checked(self):
        if self.checkbox is None:
            return False
        else:
            return self.checkbox.isChecked()


class PydmWidgetGroup(BaseWidgetGroup):
    """
    A group of pydm widgets under a single label that may be set up and reset
    as a group.
    """
    protocol = 'ca://'

    def __init__(self, widgets, pvnames, label=None, name=None, **kwargs):
        """
        Parameters
        ----------
        pvnames: list
            pvs to assign to the widgets
        """
        super().__init__(widgets, label=label, name=name,
                         pvnames=pvnames, **kwargs)

    def setup(self, *, pvnames, name=None, **kwargs):
        """
        In addition to base setup, assign pv names.
        """
        super().setup(name=name, **kwargs)
        if pvnames is None:
            pvnames = [None] * len(self.widgets)
        for widget, pvname in zip(self.widgets, pvnames):
            if pvname is None:
                chan = ''
            else:
                chan = self.protocol + pvname
            try:
                widget.setChannel(chan)
            except:
                widget.channel = chan

    def change_pvs(self, pvnames, name=None, **kwargs):
        """
        Swap active pv names and manage connections
        """
        self.clear_connections()
        self.setup(pvnames=pvnames, name=name, **kwargs)
        self.create_connections()

    def clear_connections(self):
        """
        Tell pydm to drop own pv connections.
        """
        QApp = QCoreApplication.instance()
        for widget in self.widgets:
            QApp.close_widget_connections(widget)
            widget._channels = None

    def create_connections(self):
        """
        Tell pydm to establish own pv connections.
        """
        QApp = QCoreApplication.instance()
        for widget in self.widgets:
            QApp.establish_widget_connections(widget)


class ObjWidgetGroup(PydmWidgetGroup):
    """
    A group of pydm widgets that get their channels from an object that can be
    stripped out and replaced to change context, provided the class is the
    same.
    """
    def __init__(self, widgets, attrs, obj, label=None, **kwargs):
        """
        Parameters
        ----------
        attrs: list
            list of attribute strings to pull from obj e.g. 'centroid.x'

        obj: object
            Any object that holds ophyd EpicsSignal objects that have pvname
            fields that we can use to send pvname info to pydm
        """
        self.attrs = attrs
        self.obj = obj
        if obj is None:
            name = None
        else:
            name = obj.name
        pvnames = self.get_pvnames(obj)
        super().__init__(widgets, pvnames, label=label, name=name,
                         **kwargs)

    def change_obj(self, obj, **kwargs):
        """
        Swap the active object and fix connections

        Parameters
        ----------
        obj: object
            The new object
        """
        self.obj = obj
        pvnames = self.get_pvnames(obj)
        if obj is None:
            name = None
        else:
            name = obj.name
        self.change_pvs(pvnames, name=name, **kwargs)

    def get_pvnames(self, obj):
        """
        Given an object, return the pvnames based on self.attrs
        """
        if obj is None:
            return None
        pvnames = []
        for attr in self.attrs:
            sig = self.nested_getattr(obj, attr)
            try:
                pvnames.append(sig.pvname)
            except AttributeError:
                pvnames.append(None)
        return pvnames

    def nested_getattr(self, obj, attr):
        """
        Do a getattr more than one level deep, splitting on '.'
        """
        steps = attr.split('.')
        for step in steps:
            obj = getattr(obj, step)
        return obj


class ImgObjWidget(ObjWidgetGroup):
    """
    Macros to set up the image widget channels from opyhd areadetector obj.
    This also includes all of the centroid stuff.
    """
    def __init__(self, img_widget, img_obj, cent_x_widget, cent_y_widget,
                 delta_x_widget, delta_y_widget, label, goals_source,
                 rotation=0):
        self.cent_x_widget = cent_x_widget
        self.cent_y_widget = cent_y_widget
        self.delta_x_widget = delta_x_widget
        self.delta_y_widget = delta_y_widget
        self.goals_source = goals_source
        self.xpos = 0
        self.ypos = 0
        attrs = ['detector.image2.width',
                 'detector.image2.array_data']
        super().__init__([img_widget], attrs, img_obj, label=label,
                         rotation=rotation)

    def setup(self, *, pvnames, name=None, rotation=0, **kwargs):
        BaseWidgetGroup.setup(self, name=name)
        try:
            self.cent_x.clear_sub(self.update_centroid)
            self.cent_y.clear_sub(self.update_centroid)
        except (AttributeError, ValueError):
            pass
        self.rotation = rotation
        rot_info = ad_stats_x_axis_rot(self.obj, rotation)
        self.size_x = rot_info['x_size'].value
        self.size_y = rot_info['y_size'].value
        self.cent_x = rot_info['x_cent']
        self.cent_y = rot_info['y_cent']
        self.mod_x = rot_info['mod_x']
        self.mod_y = rot_info['mod_y']
        img_widget = self.widgets[0]
        width_pv = pvnames[0]
        image_pv = pvnames[1]
        image_item = img_widget.getImageItem()
        image_item.setTransformOriginPoint(self.raw_size_x//2,
                                           self.raw_size_y//2)
        image_item.setRotation(rotation)
        view = img_widget.getView()
        view.setRange(xRange=(0, self.raw_size_x),
                      yRange=(0, self.raw_size_y),
                      padding=0.0)
        view.setLimits(xMin=0, xMax=self.raw_size_x,
                       yMin=0, yMax=self.raw_size_y)
        img_widget.resetImageChannel()
        img_widget.resetWidthChannel()
        img_widget.setMinimumWidth(self.size_x)
        img_widget.setMinimumHeight(self.size_y)
        if width_pv is None:
            width_channel = ''
        else:
            width_channel = self.protocol + width_pv
        if image_pv is None:
            image_channel = ''
        else:
            image_channel = self.protocol + image_pv
        img_widget.setWidthChannel(width_channel)
        img_widget.setImageChannel(image_channel)
        self.cent_x.subscribe(self.update_centroid)
        self.cent_y.subscribe(self.update_centroid)

    def update_centroid(self, *args, **kwargs):
        xpos = self.cent_x.value
        ypos = self.cent_y.value
        if self.mod_x is not None and xpos not in (0, None):
            xpos = self.mod_x - xpos
        if self.mod_y is not None and ypos not in (0, None):
            ypos = self.mod_y - ypos
        if xpos is not None:
            self.cent_x_widget.setText("{:.1f}".format(xpos))
            self.xpos = xpos
        if ypos is not None:
            self.cent_y_widget.setText("{:.1f}".format(ypos))
            self.ypos = ypos
        self.update_deltas()

    def update_deltas(self, *args, **kwargs):
        goal = self.goals_source.goal()
        if goal is None:
            self.delta_x_widget.clear()
        else:
            self.delta_x_widget.setText("{:.1f}".format(self.xpos - goal))
        self.delta_y_widget.clear()

    @property
    def size(self):
        return (self.size_x, self.size_y)

    @property
    def raw_size_x(self):
        return self.obj.detector.cam.array_size.array_size_x.value

    @property
    def raw_size_y(self):
        return self.obj.detector.cam.array_size.array_size_y.value