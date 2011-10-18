from __future__ import with_statement
import sys
import os
from datetime import datetime
import threading
import codecs

from java.util import Date
from java.util import Vector
from java.io import StringReader

import customhandlers
from util import to_jdate, to_pdate, to_jtime, to_ptime, to_vect
    
from setup import init_classpath, init_jr_engine
import logging
init_classpath()
init_jr_engine()

import com.xhaus.jyson.JysonCodec as json

from org.javarosa.xform.parse import XFormParser
from org.javarosa.form.api import FormEntryModel, FormEntryController
from org.javarosa.core.model import Constants, FormIndex
from org.javarosa.core.model.data import *
from org.javarosa.core.model.data.helper import Selection
from org.javarosa.model.xform import XFormSerializingVisitor as FormSerializer

class NoSuchSession(Exception):
  pass

class global_state_mgr:
  instances = {}
  instance_id_counter = 0

  session_cache = {}
  session_id_counter = 0
  
  def __init__(self):
    self.lock = threading.Lock()
  
  def new_session(self, xfsess):
    with self.lock:
      self.session_id_counter += 1
      self.session_cache[self.session_id_counter] = xfsess
    return self.session_id_counter
  
  def get_session(self, session_id):
    with self.lock:
      try:
        return self.session_cache[session_id]
      except KeyError:
        raise NoSuchSession()
    
  #todo: we're not calling this currently, but should, or else xform sessions will hang around in memory forever    
  def destroy_session(self, session_id):
    with self.lock:
      try:
        del self.session_cache[session_id]
      except KeyError:
        raise NoSuchSession()
    
  def save_instance(self, data):        
    with self.lock:
      self.instance_id_counter += 1
      self.instances[self.instance_id_counter] = data
    return self.instance_id_counter

  #todo: add ways to get xml, delete xml, and option not to save xml at all

global_state = global_state_mgr()
  

def load_form(xform, instance=None, extensions=[], preload_data={}):
    form = XFormParser(StringReader(xform)).parse()
    if instance != None:
        XFormParser.loadXmlInstance(form, StringReader(instance))

    customhandlers.attach_handlers(form, preload_data, extensions)

    form.initialize(instance == None)
    return form

class SequencingException(Exception):
    pass

class XFormSession:
    def __init__(self, xform_raw=None, xform_path=None, instance_raw=None, instance_path=None,
                 preload_data={}, extensions=[]):
        self.lock = threading.Lock()

        def content_or_path(content, path):
            if content != None:
                return content
            elif path != None:
                with codecs.open(path, encoding='utf-8') as f:
                    return f.read()
            else:
                return None

        xform = content_or_path(xform_raw, xform_path)
        instance = content_or_path(instance_raw, instance_path)

        self.form = load_form(xform, instance, extensions, preload_data)
        self.fem = FormEntryModel(self.form, FormEntryModel.REPEAT_STRUCTURE_NON_LINEAR)
        self.fec = FormEntryController(self.fem)
        self._parse_current_event()

    def __enter__(self):
        if not self.lock.acquire(False):
            raise SequencingException()
        return self

    def __exit__(self, *_):
        self.lock.release()
 
    def output(self):
        if self.cur_event['type'] != 'form-complete':
            #warn that not at end of form
            pass

        instance_bytes = FormSerializer().serializeInstance(self.form.getInstance())
        return unicode(''.join(chr(b) for b in instance_bytes.tolist()), 'utf-8')
    
    def walk(self):
        form_ix = FormIndex.createBeginningOfFormIndex()
        tree = []
        self._walk(form_ix, tree)
        return tree

    def _walk(self, parent_ix, siblings):
      def ix_in_scope(form_ix):
        if form_ix.isEndOfFormIndex():
          return False
        elif parent_ix.isBeginningOfFormIndex():
          return True
        else:
          return FormIndex.isSubElement(parent_ix, form_ix)

      form_ix = self.fem.incrementIndex(parent_ix, True)
      while ix_in_scope(form_ix):
        evt = self.__parse_event(form_ix)
        if evt['type'] == 'sub-group':
          presentation_group = (evt['caption'] != None)
          if presentation_group:
            siblings.append(evt)
            evt['children'] = []
          form_ix = self._walk(form_ix, evt['children'] if presentation_group else siblings)
        elif evt['type'] == 'repeat-juncture':
          siblings.append(evt)
          evt['children'] = []
          for i in range(0, self.fem.getForm().getNumRepetitions(form_ix)):
            subevt = {
              'type': 'sub-group',
              'ix': self.fem.getForm().descendIntoRepeat(form_ix, i),
              'caption': evt['repetitions'][i],
              'repeatable': True,
              'children': [],
            }
            evt['children'].append(subevt)
            self._walk(subevt['ix'], subevt['children'])
          for key in ['repetitions', 'del-choice', 'del-header', 'done-choice']:
            del evt[key]
          form_ix = self.fem.incrementIndex(form_ix, True)
        else:
          siblings.append(evt)
          form_ix = self.fem.incrementIndex(form_ix, True)

      return form_ix

    def _parse_current_event(self):
        self.cur_event = self.__parse_event(self.fem.getFormIndex())
        return self.cur_event
    
    def __parse_event (self, form_ix):
        event = {'ix': form_ix}
      
        status = self.fem.getEvent(form_ix)
        if status == self.fec.EVENT_BEGINNING_OF_FORM:
            event['type'] = 'form-start'
        elif status == self.fec.EVENT_END_OF_FORM:
            event['type'] = 'form-complete'
            self.fem.getForm().postProcessInstance() 
        elif status == self.fec.EVENT_QUESTION:
            event['type'] = 'question'
            self._parse_question(event)
        elif status == self.fec.EVENT_REPEAT_JUNCTURE:
            event['type'] = 'repeat-juncture'
            self._parse_repeat_juncture(event)
        else:
            event['type'] = 'sub-group'
            event['caption'] = self.fem.getCaptionPrompt(form_ix).getLongText()
            if status == self.fec.EVENT_GROUP:
                event['repeatable'] = False
            elif status == self.fec.EVENT_REPEAT:
                event['repeatable'] = True
                event['exists'] = True
            elif status == self.fec.EVENT_PROMPT_NEW_REPEAT: #obsolete
                event['repeatable'] = True
                event['exists'] = False

        return event

    def _get_question_choices(self, q):
        return [choice(q, ch) for ch in q.getSelectChoices()]

    def _parse_style_info(self, rawstyle):
        info = {}

        if rawstyle != None:
            info['raw'] = rawstyle
            try:
                info.update([[p.strip() for p in f.split(':')][:2] for f in rawstyle.split(';') if f.strip()])
            except ValueError:
                pass

        return info

    def _parse_question (self, event):
        q = self.fem.getQuestionPrompt(event['ix'])

        event['caption'] = q.getLongText()
        event['help'] = q.getHelpText()
        event['style'] = self._parse_style_info(q.getAppearanceHint())

        if q.getControlType() == Constants.CONTROL_TRIGGER:
            event['datatype'] = 'info'
        else:
            event['datatype'] = {
              Constants.DATATYPE_NULL: 'str',
              Constants.DATATYPE_TEXT: 'str',
              Constants.DATATYPE_INTEGER: 'int',
              Constants.DATATYPE_DECIMAL: 'float',
              Constants.DATATYPE_DATE: 'date',
              Constants.DATATYPE_TIME: 'time',
              Constants.DATATYPE_CHOICE: 'select',
              Constants.DATATYPE_CHOICE_LIST: 'multiselect',

              # not supported yet
              Constants.DATATYPE_DATE_TIME: 'datetime',
              Constants.DATATYPE_GEOPOINT: 'geo',
              Constants.DATATYPE_BARCODE: 'barcode',
              Constants.DATATYPE_BINARY: 'binary',
              Constants.DATATYPE_LONG: 'longint',              
            }[q.getDataType()]

            if event['datatype'] in ('select', 'multiselect'):
                event['choices'] = self._get_question_choices(q)

            event['required'] = q.isRequired()

            value = q.getAnswerValue()
            if value == None:
                event['answer'] = None
            elif event['datatype'] == 'int':
                event['answer'] = value.getValue()
            elif event['datatype'] == 'float':
                event['answer'] = value.getValue()
            elif event['datatype'] == 'str':
                event['answer'] = value.getValue()
            elif event['datatype'] == 'date':
                event['answer'] = to_pdate(value.getValue())
            elif event['datatype'] == 'time':
                event['answer'] = to_ptime(value.getValue())              
            elif event['datatype'] == 'select':
                event['answer'] = value.getValue().index + 1
            elif event['datatype'] == 'multiselect':
                event['answer'] = [sel.index + 1 for sel in value.getValue()]

    def _parse_repeat_juncture(self, event):
        r = self.fem.getCaptionPrompt(event['ix'])
        ro = r.getRepeatOptions()

        event['main-header'] = ro.header
        event['repetitions'] = list(r.getRepetitionsText())

        event['add-choice'] = ro.add
        event['del-choice'] = ro.delete
        event['del-header'] = ro.delete_header
        event['done-choice'] = ro.done

    def next_event (self):
        self.fec.stepToNextEvent()
        return self._parse_current_event()

    def back_event (self):
        self.fec.stepToPreviousEvent()
        return self._parse_current_event()

    def answer_question (self, answer):
        if self.cur_event['type'] != 'question':
            raise ValueError('not currently on a question')

        datatype = self.cur_event['datatype']
        if answer == None or str(answer).strip() == '' or answer == [] or datatype == 'info':
            ans = None
        elif datatype == 'int':
            ans = IntegerData(int(answer))
        elif datatype == 'float':
            ans = DecimalData(float(answer))
        elif datatype == 'str':
            ans = StringData(str(answer))
        elif datatype == 'date':
            ans = DateData(to_jdate(datetime.strptime(str(answer), '%Y-%m-%d').date()))
        elif datatype == 'time':
            ans = TimeData(to_jtime(datetime.strptime(str(answer), '%H:%M').time()))
        elif datatype == 'select':
            ans = SelectOneData(self.cur_event['choices'][int(answer) - 1].to_sel())
        elif datatype == 'multiselect':
            if hasattr(answer, '__iter__'):
                ans_list = answer
            else:
                ans_list = str(answer).split()
            ans = SelectMultiData(to_vect(self.cur_event['choices'][int(k) - 1].to_sel() for k in ans_list))

        result = self.fec.answerQuestion(ans)
        if result == self.fec.ANSWER_REQUIRED_BUT_EMPTY:
            return {'status': 'error', 'type': 'required'}
        elif result == self.fec.ANSWER_CONSTRAINT_VIOLATED:
            q = self.fem.getQuestionPrompt()
            return {'status': 'error', 'type': 'constraint', 'reason': q.getConstraintText()}
        elif result == self.fec.ANSWER_OK:
            return {'status': 'success'}

    def descend_repeat (self, ix=None):
        if ix:
            self.fec.descendIntoRepeat(ix - 1)
        else:
            self.fec.descendIntoNewRepeat()

        return self._parse_current_event()

    def delete_repeat (self, ix):
        self.fec.deleteRepeat(ix - 1)
        return self._parse_current_event()

    #sequential (old-style) repeats only
    def new_repetition (self):
      #currently in the form api this always succeeds, but theoretically there could
      #be unsatisfied constraints that make it fail. how to handle them here?
      self.fec.newRepeat(self.fem.getFormIndex())

class choice(object):
    def __init__(self, q, select_choice):
        self.q = q
        self.select_choice = select_choice

    def to_sel(self):
        return Selection(self.select_choice)

    def __repr__(self):
        return self.q.getSelectChoiceText(self.select_choice)

    def __json__(self):
        return json.dumps(repr(self))

def nav(resp, xfsess, nav_mode, ev_next=None):
    if nav_mode == 'prompt':
        if ev_next is None:
            ev_next = next_event(xfsess)
        navinfo = {'event': ev_next}
    elif nav_mode == 'fao':
        #debug
        print '=== walking ==='
        print_tree(xfsess.walk())
        print '==============='

        navinfo = {'tree': xfsess.walk()}
    resp.update(navinfo)
    return resp

def open_form(form_name, instance_xml=None, extensions=[], preload_data={}, nav_mode='prompt'):
    if not os.path.exists(form_name):
        return {'error': 'no form found at %s' % form_name}

    xfsess = XFormSession(xform_path=form_name, instance_raw=instance_xml, preload_data=preload_data, extensions=extensions)
    sess_id = global_state.new_session(xfsess)
    return nav({'session_id': sess_id}, xfsess, nav_mode)

def answer_question (session_id, answer, nav_mode='prompt'):
    with global_state.get_session(session_id) as xfsess:
        result = xfsess.answer_question(answer)
        if result['status'] == 'success':
            return nav({'status': 'accepted'}, xfsess, nav_mode)
        else:
            result['status'] = 'validation-error'
            return result

def edit_repeat (session_id, ix):
    with global_state.get_session(session_id) as xfsess:
        ev = xfsess.descend_repeat(ix)
        return {'event': ev}

def new_repeat (session_id, nav_mode='prompt'):
    with global_state.get_session(session_id) as xfsess:
        ev = xfsess.descend_repeat()
        return nav({}, xfsess, nav_mode, ev)

def delete_repeat (session_id, ix, nav_mode='prompt'):
    with global_state.get_session(session_id) as xfsess:
        ev = xfsess.delete_repeat(ix)
        return nav({}, xfsess, nav_mode, ev)

#sequential (old-style) repeats only
def new_repetition (session_id):
    with global_state.get_session(session_id) as xfsess:
        #new repeat creation currently cannot fail, so just blindly proceed to the next event
        xfsess.new_repetition()
        return {'event': next_event(xfsess)}

def skip_next (session_id):
    with global_state.get_session(session_id) as xfsess:
        return {'event': next_event(xfsess)}

def go_back (session_id):
    with global_state.get_session(session_id) as xfsess:
        (at_start, event) = prev_event(xfsess)
        return {'event': event, 'at-start': at_start}

# fao mode only
# INCOMPLETE
def complete_form(session_id, validate_all=True):
    if validate_all:
        pass
        # do final validation here?
    
    #copy 'form complete' branch of next_event() ?

def next_event (xfsess):
    ev = xfsess.next_event()
    if ev['type'] != 'form-complete':
        return ev
    else:
        (instance_id, xml) = save_form(xfsess)
        ev['save-id'] = instance_id
        ev['output'] = xml
        return ev

def prev_event (xfsess):
    at_start, ev = False, xfsess.back_event()
    if ev['type'] == 'form-start':
        at_start, ev = True, xfsess.next_event()
    return at_start, ev

def save_form (xfsess):
    xml = xfsess.output()
    instance_id = global_state.save_instance(xml)
    return (instance_id, xml)






# debugging

import pprint
pp = pprint.PrettyPrinter(indent=2)
def print_tree(tree):
    pp.pprint(tree)
