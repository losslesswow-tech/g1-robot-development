"""Add speech controls to the observed compiled task editor, preserving its imports."""
from pathlib import Path
from .patch_task import once


def patch_frontend(source):
    source = once(source, 'Fn=[{value:"E",label:"导航"}', 'Fn=[{value:"J",label:"语音播报"},{value:"E",label:"导航"}')
    source = once(source, 'fo(d)?i.jsx(ne.Item,{name:[t,"target","arg"]',
                  'd==="J"?i.jsx(G1TaskSpeech,{path:e,fieldName:t}):null,fo(d)?i.jsx(ne.Item,{name:[t,"target","arg"]')
    source += '\n' + Path(__file__).with_name('task_speech_ui.js').read_text(encoding='utf-8')
    return source
