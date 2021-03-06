from math import degrees, radians
import socket
import time
import logging
import threading

import bpy
from . import handheld_panel

log = logging.getLogger(__name__)
# set verbosity level
log.setLevel(level=logging.INFO)

# running script multiple times adds new handler every time
if len(log.handlers) == 0:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)  # set verbosity level for handler
    formatter = logging.Formatter('%(levelname)s:%(threadName)s:%(message)s')
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


class HandheldClient(threading.Thread):
    
    def __init__(self, context, acc_transform=None, rot_transform=None):
        threading.Thread.__init__(self, daemon=True)
        self.handheld_data = context.scene.handheld_data
        # for stopping receiving loop
        self._receiving = False   
        # for syncing acces to deltas
        self.lock_loc_rot = threading.Lock()  
        self._delta_loc = [0, 0, 0]
        self._delta_rot = [0, 0, 0]
        self._last_parsed_packet_time = None  # used for calculating location from acceleration
        # income data may need some user defined processing 
        self.acc_transform = acc_transform  
        self.rot_transform = rot_transform
    
    @property
    def delta_loc(self):
        """Getter: reset delta location to [0, 0, 0] and return original"""
        tmp = self._delta_loc
        with self.lock_loc_rot:
            self._delta_loc = [0, 0, 0]
        return tmp
    
    @delta_loc.setter
    def delta_loc(self, value):
        """Setter: 3 element list/tuple required """
        for i in enumerate(self._delta_loc):
            self._delta_loc[i] = float(value[i])
        
    @property
    def delta_rot(self):
        """Getter: reset delta rotation to [0, 0, 0] and return original"""
        tmp = self._delta_rot
        with self.lock_loc_rot:
            self._delta_rot = [0, 0, 0]
        return tmp
    
    @delta_rot.setter
    def delta_rot(self, value):
        """Setter: 3 element list/tuple required (degrees)"""
        for i in enumerate(self._delta_rot):
            self._delta_rot[i] = float(value[i])
        
    def start(self):
        self._receiving = True
        threading.Thread.start(self)
         
    def stop(self):
        self._receiving = False
        
    def run(self):
        """Establishes connection, receives and parses data until self.stop() is called.
            Updates delta_loc and delta_rot based on received data
        """
        try:
            client_socket = socket.socket()
            client_socket.connect((self.handheld_data.host, self.handheld_data.port))
        except socket.error:
            log.exception("Unable to open connection")
            self.report({'ERROR'}, "Unable to open connection. Look at console for more info...")
        else:
            log.info("Connection established with: " + self.handheld_data.host)
            handheld_panel.is_connected = True
            # data = ''
            while self._receiving:
                data = client_socket.recv(1024).decode()
                if data is '':  # end if received empty message
                    self._receiving = False
                self.parse_data(data)        
            client_socket.close()
            log.info("Connection closed({})".format(self.handheld_data.host))

    def parse_data(self, data):
        """Split income data into single datagrams, calculate and apply deltas to delta_loc, delta_rot"""
        # data ends with ';' what leaves empty string at the end 
        data = data.split(';')[:-1]
        for single_datagram in data:
            acc, rot, time = self.parse_single_datagram(single_datagram)
            with self.lock_loc_rot:
                for i, delta in enumerate(self.calculate_loc_delta(acc, time)):
                    self._delta_loc[i] += delta
                for i, delta in enumerate(self.calculate_rot_delta(rot, time)):
                    self._delta_rot[i] += delta
        log.debug("current location delta: {}, rotation delta: {}".format(self._delta_loc, self._delta_rot))
         
    def parse_single_datagram(self, single_datagram):
        data = single_datagram.split()
        acc = [float(i) for i in data[0:3]]
        rot = [float(i) for i in data[3:6]]
        time = float(data[6])
        
        # apply user defined functions if exist
        if self.acc_transform != None:
            acc = self.acc_transform(acc)
            
        if self.rot_transform != None:
            rot =  self.rot_transform(rot)
            
        return acc, rot, time
         
    def calculate_loc_delta(self, acc, time):
        """Changes acceleration to position change in time: s = 2*a/t^2 - constant interpolation"""
        if self._last_parsed_packet_time is None:
            self._last_parsed_packet_time = time
            return [0,0,0]
        time_delta = time - self._last_parsed_packet_time 
        self._last_parsed_packet_time = time
        # use scale to limit effect
        translate = lambda x: self.handheld_data.scale*2*x/time_delta**2
        return map(translate, acc)

    def calculate_rot_delta(self, rot, time):
        """Unused """
        return rot


 
class HandheldAnimate(bpy.types.Operator):
    """Main operator responsible for starting client thread, shortcuts, handling teardown """
    bl_idname = "handheld.animate"
    bl_label = "Modal Timer Operator"
    
    status = "Connect"  # used for button in panel
    running = False  # used for poll func 
    connection_thread = None
    timer = None
    handler_exists = False
    
    
    @classmethod
    def poll(cls, context):
        """ Disable creating new instances of operator if operator is running (and make button gray)"""
        return not HandheldAnimate.running and context.area.type == 'VIEW_3D'
    
    def modal(self, context, event):        
        if event.type in {'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}
        
        status_text = '(ESC) to exit, '
        if self.handler_exists:
            status_text += "(Y) to stop playback, "
        else:
            status_text += "(Y) to start animation, "
       
        if event.type == 'Y' and event.value == 'PRESS':
            log.info("timer: {}, handler: {}, event: {}-{}".format(self.timer, self.handler_exists, event.type, event.value))
            if not self.handler_exists:
                log.debug("Switching to per frame update")
                HandheldAnimate.status = "Running on frame changed"
                bpy.app.handlers.frame_change_pre.append(self.update_object_on_frame_changed)
                bpy.ops.screen.animation_play()
                if self.timer != None:
                    wm = context.window_manager
                    wm.event_timer_remove(self.timer)
                    del self.timer  # necessary ??
                self.handler_exists = True
            else: 
                log.debug("Switching to static update")
                HandheldAnimate.status = "Running statically" 
                if self.timer == None:
                    wm = context.window_manager
                    self.timer = wm.event_timer_add(1/context.scene.render.fps, context.window)
                bpy.ops.screen.animation_play()
                bpy.app.handlers.frame_change_pre.remove(self.update_object_on_frame_changed)
                self.handler_exists = False  

        if event.type == 'TIMER':
            HandheldAnimate.status = "Running statically"
            name = context.scene.handheld_data.selected_object
            self.update_object(
                bpy.data.objects[name], 
                self.connection_thread.delta_loc, 
                self.connection_thread.delta_rot)

        context.area.header_text_set(status_text)
        return {'PASS_THROUGH'}

    def execute(self, context):
        HandheldAnimate.running = True
        self.connection_thread = HandheldClient(context)
        self.connection_thread.start()
        # add timer for static updates: 
        wm = context.window_manager
        self.timer = wm.event_timer_add(1/context.scene.render.fps, context.window)
        # register operator as modal:
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        if self.handler_exists:
            bpy.ops.screen.animation_play()
            bpy.app.handlers.frame_change_pre.remove(self.update_object_on_frame_changed)
        else:
            wm = context.window_manager
            wm.event_timer_remove(self.timer)
        
        context.area.header_text_set()
        HandheldAnimate.status = "Connect"
        HandheldAnimate.running = False
        self.connection_thread.stop()
    
    def update_object(self, obj, delta_loc, delta_rot):
        loc = obj.location
        for i, xyz in enumerate(delta_loc):
            loc[i] += xyz
            
        rot = obj.rotation_euler
        for i, xyz in enumerate(delta_rot):
            rot[i] += radians(xyz)

            
    def update_object_on_frame_changed(self, scene):
        name = scene.handheld_data.selected_object
        obj = bpy.data.objects[name]
        try: 
            self.update_object(
                obj, 
                self.connection_thread.delta_loc, 
                self.connection_thread.delta_rot)
        except:
            bpy.app.handlers.frame_change_pre.pop()
        else:
            obj.keyframe_insert(data_path='location')
            obj.keyframe_insert(data_path='rotation_euler')