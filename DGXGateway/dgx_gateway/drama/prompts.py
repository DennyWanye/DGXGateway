"""Workbench rendering of preserved model drafts into the H3 input contract."""
import re

from ..h3_prompt import compile_prompt


def prepare_video_prompt(value, inputs):
    notes = []
    rendered = value
    if isinstance(value, dict) and inputs.get('soundMode', 'silent') == 'silent':
        sound = value.get('overall_soundscape')
        # The user's selected sound mode is authoritative even when the writer
        # omits the negative clause. Reject explicit conflicting speech first.
        if isinstance(sound, str):
            remainder = re.sub(r'(?:无|没有|禁止|不允许|不要)(?:任何)?(?:对白|旁白|人声|说话|喊叫|台词)', '', sound)
            remainder = re.sub(r'\b(?:no|without)\s+(?:(?:any|other|human)\s+)?(?:dialogue|speech|voices?|narration|shouting|singing)\b', '', remainder, flags=re.I)
            if re.search(r'说话|回答|对白|旁白|人声|喊叫|台词|\b(?:dialogue|speech|voices?|narration|speaks?|says?|shout(?:s|ing)?|sing(?:s|ing)?)\b', remainder, re.I):
                raise ValueError('声音描述含人声，与无对白模式冲突；请修改声音要求')
        if isinstance(sound, str) and not re.search(r'no (?:dialogue|speech|voices)', sound, re.I):
            rendered = {**value, 'overall_soundscape': 'No dialogue, no speech, no voices. ' + sound}
            notes.append('按你选择的无对白模式，最终提示词已补充英文禁说话约束；模型原始输出完整保留。')
    # Byte budget, unexpected dialogue and nonexistent references remain hard gates.
    return compile_prompt(rendered, inputs), notes
