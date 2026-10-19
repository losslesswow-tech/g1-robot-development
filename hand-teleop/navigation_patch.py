"""Patch exact observed task anchors; never executes tasks or reloads services."""
import ast


def once(source, before, after):
    if source.count(before)!=1:raise ValueError('Changed navigation anchor: '+before[:80])
    return source.replace(before,after,1)


def patch_task(source):
    source=preserve_task_result(source)
    source=once(source,'import requests\n','import requests\nfrom g1_task_hand import TaskHand, validate_hand_steps\n')
    source=once(source,'register_voice_routes(task_blueprint)\n','''register_voice_routes(task_blueprint)

@task_blueprint.route('/hand_action_catalog', methods=['GET'])
def hand_action_catalog():
    return jsonify({'actions': [{'id': 'right_index_extend', 'name': '伸食指',
                     'action': 'U', 'side': 'R', 'channel': 3, 'raw_target': 1000,
                     'other_channels': 'hold_device_command_targets', 'retries': 0,
                     'completion': 'right_index_below8_and_targets_unchanged'}]})
''')
    source=once(source,'        self.backend_post = rospy.get_param("/x_nav/backend_post", 9000)\n',
                '        self.backend_post = rospy.get_param("/x_nav/backend_post", 9000)\n        self.hand = TaskHand()\n')
    assert source.count('        validate_speech_steps(data)\n')==2
    source=source.replace('        validate_speech_steps(data)\n','        validate_speech_steps(data)\n        validate_hand_steps(data)\n')
    source=once(source,'            voice_managed = validate_speech_steps(self.all_task[run_id])\n',
                '            voice_managed = validate_speech_steps(self.all_task[run_id])\n            validate_hand_steps(self.all_task[run_id])\n')
    source=once(source,'        self.voice.start(voice_managed)\n','''        try:
            self.hand.start()
        except RuntimeError as error:
            self.task_state_str = str(error)
            return
        self.voice.start(voice_managed)
''')
    source=once(source,'        self.voice.cancel()\n','        self.hand.cancel()\n        self.voice.cancel()\n')
    source=once(source,'    task_manager.voice.pause()\n','    task_manager.hand.pause()\n    task_manager.voice.pause()\n')
    source=once(source,'    task_manager.voice.resume()\n','    task_manager.hand.resume()\n    task_manager.voice.resume()\n')
    source=once(source,'            elif action == "J":\n                param = dict(target)\n',
                '            elif action in ("J", "U"):\n                param = dict(target)\n')
    source=once(source,'        elif current_action == "J":\n','''        elif current_action == "U":
            hand_run_before = self.hand.run_id
            hand_step_before = self.task_index
            try:
                hand_result = self.hand.step(self.current_loop, self.task_index, current_action_param)
            except Exception as error:
                self.reset_task(f"伸食指步骤无效，任务已停止: {error}")
                return
            if self.hand.run_id != hand_run_before or self.task_index != hand_step_before:
                return
            if hand_result['state'] == 'done':
                self.task_index += 1
                self.time_index = 0
                self.task_state_str = f"伸食指完成 任务进度: {self.task_index}/{len(self.task_buf)}"
            elif hand_result['state'] == 'failed':
                self.reset_task(f"伸食指失败，任务已停止: {hand_result.get('error', '结果不确定，不自动重试')}")
            else:
                self.task_state_str = f"正在伸食指 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "J":
''')
    ast.parse(source,feature_version=(3,8))
    return add_hand_status(add_retract_task(source))


def preserve_task_result(source):
    return once(source, '        if not self.task_buf:\n            self.task_state_str = "无任务"\n            return\n',
                '        if not self.task_buf:\n            return  # Keep the last outcome visible until another task starts.\n')


def add_hand_status(source):
    return once(source, '\nROBOT_TASK_ACTIONS = {', '''
@task_blueprint.route('/hand_execution_status', methods=['GET'])
def hand_execution_status():
    manager = current_app.config['task_manager']
    with manager.hand.lock:
        hand_active = any(job['thread'].is_alive() for job in manager.hand.jobs.values())
    return jsonify({'task_active': bool(manager.task_buf or manager.task_error_buf),
                    'hand_active': hand_active})

ROBOT_TASK_ACTIONS = {''')


def patch_frontend(source):
    source=once(source,'Fn=[{value:"J",label:"语音播报"}',
                'Fn=[{value:"J",label:"语音播报"},{value:"U",label:"伸食指（右手）"}')
    source=once(source,'d==="J"?i.jsx(G1TaskSpeech,{path:e,fieldName:t}):null,',
                'd==="J"?i.jsx(G1TaskSpeech,{path:e,fieldName:t}):null,d==="U"?i.jsx(G1TaskHand,{path:e,fieldName:t}):null,')
    return add_retract_frontend(source+'''
function G1TaskHand({path,fieldName}) {
  const form=ne.useFormInstance();
  oe.useEffect(()=>{form.setFieldValue([...path,fieldName,"target"],{handAction:"right_index_extend"});},[form,fieldName,JSON.stringify(path)]);
  return i.jsxs("div",{children:[i.jsx(ne.Item,{name:[fieldName,"target","handAction"],initialValue:"right_index_extend",hidden:true,children:i.jsx(Pe,{})}),i.jsx("span",{children:"右手食指伸直 · 其余保持 · 无自动重试"})]});
}
''')


def add_retract_task(source):
    source=once(source,"'completion': 'right_index_below8_and_targets_unchanged'}]})", """'completion': 'right_index_below8_and_targets_unchanged'},
                    {'id': 'right_index_retract', 'name': '收食指',
                     'action': 'V', 'side': 'R', 'channel': 3, 'raw_target': 0,
                     'other_channels': 'hold_device_command_targets', 'retries': 0,
                     'completion': 'right_index_below8_and_targets_unchanged'}]})""")
    source=once(source,'        elif current_action == "U":\n',
                '        elif current_action in ("U", "V"):\n            hand_action_name = "伸食指" if current_action == "U" else "收食指"\n')
    source=once(source,'            elif action in ("J", "U"):\n',
                '            elif action in ("J", "U", "V"):\n')
    for before,after in [('f"伸食指步骤无效','f"{hand_action_name}步骤无效'),
                         ('f"伸食指完成','f"{hand_action_name}完成'),
                         ('f"伸食指失败','f"{hand_action_name}失败'),
                         ('f"正在伸食指','f"正在{hand_action_name}')]:
        source=once(source,before,after)
    ast.parse(source,feature_version=(3,8))
    return source


def add_retract_frontend(source):
    source=once(source,'{value:"U",label:"伸食指（右手）"}',
                '{value:"U",label:"伸食指（右手）"},{value:"V",label:"收食指（右手）"}')
    source=once(source,'d==="U"?i.jsx(G1TaskHand,{path:e,fieldName:t})',
                '(d==="U"||d==="V")?i.jsx(G1TaskHand,{path:e,fieldName:t,action:d})')
    source=once(source,'function G1TaskHand({path,fieldName}) {',
                'function G1TaskHand({path,fieldName,action}) {\n  const retract=action==="V";const handAction=retract?"right_index_retract":"right_index_extend";')
    source=once(source,'{handAction:"right_index_extend"});},[form,fieldName,JSON.stringify(path)]);',
                '{handAction});},[form,fieldName,JSON.stringify(path),handAction]);')
    source=once(source,'initialValue:"right_index_extend",hidden:true',
                'initialValue:handAction,hidden:true')
    source=once(source,'children:"右手食指伸直 · 其余保持 · 无自动重试"',
                'children:retract?"右手食指收回至0 · 其余保持 · 无自动重试":"右手食指伸直 · 其余保持 · 无自动重试"')
    return source
