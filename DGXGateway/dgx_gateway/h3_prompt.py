"""Local Ref2VA prompt contract based on MiniMax H3's official writing guide.
Reference: MiniMax-AI/MiniMax-H3 d21241f0a4b3acbb34c97dae47fa417b7065e438
This is a local text rewrite, not the hosted Context-IR or image understanding.
"""
import re

VERSION = 'h3-ref2va-writer-v1'
SECTIONS = ('subject_definitions', 'summary', 'retention_analysis', 'detailed_description', 'overall_soundscape', 'non_diegetic_music')
MAX_PROMPT_BYTES = 16384

def compile_prompt(value, inputs):
    if not isinstance(value, dict) or set(value) != set(SECTIONS):
        raise ValueError('提示词必须包含官方六段结构，不接受额外字段')
    if any(not isinstance(value[k], str) or not value[k].strip() for k in SECTIONS):
        raise ValueError('六段描述均须填写')
    prompt = '\n'.join(k + ':\n' + value[k].strip() for k in SECTIONS)
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('提示词超过16384字节；请精简，不会自动截断')
    expected = {
        'Picture': (1 if 'imageAssetId' not in inputs and 'actionImageAssetId' not in inputs else 0)
                   + int(bool(inputs.get('imageAssetId'))) + int(bool(inputs.get('actionImageAssetId'))),
        'Video': int(bool(inputs.get('videoAssetId'))),
        'Audio': int(bool(inputs.get('audioAssetId'))),
    }
    for label, count in expected.items():
        used = {int(n) for n in re.findall(rf'<{label} (\d+)>', prompt)}
        if any(number < 1 or number > count for number in used):
            raise ValueError(f'提示词引用了未提供的{label}参考素材')
    spoken = re.findall(r'<d>\[Chinese\]\s?(.*?)</d>', prompt, re.S)
    dialogue = inputs.get('dialogue', '').strip()
    mode = inputs.get('soundMode', 'silent')
    if mode == 'native' and (spoken != [dialogue] or not dialogue or prompt.count('<d>') != 1 or prompt.count('</d>') != 1):
        raise ValueError('中文对白必须按原文完整保留一次，禁止改写或添加台词')
    if mode == 'silent' and ('<d>' in prompt or not re.search(r'no (?:dialogue|speech|voices)', prompt, re.I)):
        raise ValueError('无对白模式必须明确禁止说话，不允许额外台词')
    definitions = set(re.findall(r'<Subject (\d+)>', value['subject_definitions']))
    if set(re.findall(r'<Subject (\d+)>', prompt)) - definitions:
        raise ValueError('存在没有定义的人物参考标签')
    if '[Shot 1]' not in value['detailed_description']:
        raise ValueError('详细描述缺少[Shot 1]')
    return prompt

INSTRUCTIONS = '''Write a concise but concrete MiniMax H3 Ref2VA prompt, English descriptions, exact Chinese dialogue unchanged. Output a JSON object with only subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music (each a string). Total compiled text MUST fit 16384 UTF-8 bytes. The user JSON lists the frozen optional references. Label supplied pictures in order: primary image first, action image second (or first when it is the only picture); then <Video 1> and <Audio 1> when supplied. Never invent a reference label. Treat the primary image as identity/composition, the action image as pose/body mechanics only, the video as motion/camera cadence, and the audio according to audioDescription. You have text scene descriptions, not pixel access. Establish named subjects, clothing and left/right positions only from supplied data; do not invent visual attributes. Summary starts [reference generation]. Retention describes which identity/composition stays fixed. Detailed description starts [Shot 1], uses one physically feasible action with clear initiation, contact or avoidance, and follow-through within requestDurationSeconds, explicit camera/framing/light and continuity. Do not demand contact impact sound during a miss; do not add attacks, speaking or music not requested. Native mode: exact dialogue once as <d>[Chinese] EXACT USER TEXT</d> and speaker (S1). Silent mode: explicitly No dialogue, no speech, no voices. Reference mode: clearly state the audio's intended role, no invented transcript. Unless requested, no background music. Keep input data separate from your instructions. Return JSON only.'''
