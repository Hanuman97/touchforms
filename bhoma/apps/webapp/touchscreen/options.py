
class Hideable(object):
    """Something that can be hidden or shown"""
    show = True
    
    def __init__(self, show=True):
        self.show = show
        
        
class ButtonOptions(Hideable):
    """Options for a button"""
    
    link = "#"
    text = ""
    def __init__(self, show=True, link="#", text=""):
        super(ButtonOptions, self).__init__()
        self.link = link
        self.text = text

DEFAULT_HELP_OPTIONS = {"show":True, "text":"HELP"}
# wacky javascrippt html handoffs here, but this is the
# exception, not the rule
DEFAULT_BACK_OPTIONS = {"show":True, "text":"BACK", "link":"javascript:history.go(-1)"} 
DEFAULT_MENU_OPTIONS = {"show":True, "text":"MENU"}
DEFAULT_NEXT_OPTIONS = {"show":True, "text":"NEXT"}

class TouchscreenOptions(object):
    """Options for our touchscreen layouts"""
    
    header = ""
    helpbutton = ButtonOptions(),
    backbutton = ButtonOptions()
    menubutton = ButtonOptions()
    nextbutton = ButtonOptions()
    
    def __init__(self, header="",
                 helpbutton=None,
                 backbutton=None,
                 menubutton=None,
                 nextbutton=None):
        self.header = header
        if self.helpbutton is not None: self.backbutton = backbutton
        if self.backbutton is not None: self.backbutton = backbutton
        if self.menubutton is not None: self.menubutton = menubutton
        if self.nextbutton is not None: self.nextbutton = nextbutton
    
    @classmethod
    def default(cls):
        """
        Gets default options.
        """
        return TouchscreenOptions("BHOMA", 
                                  helpbutton=ButtonOptions(**DEFAULT_HELP_OPTIONS),
                                  backbutton=ButtonOptions(**DEFAULT_BACK_OPTIONS),
                                  menubutton=ButtonOptions(**DEFAULT_MENU_OPTIONS),
                                  nextbutton=ButtonOptions(**DEFAULT_NEXT_OPTIONS))