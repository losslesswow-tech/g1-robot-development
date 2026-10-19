"""Produce a reviewed patch for the observed navigation task module; never execute it."""
import ast
import hashlib
from pathlib import Path


def once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("Navigation source differs at anchor: " + before[:90])
    return source.replace(before, after, 1)


def patch_task(source):
    source = once(source, "import requests\n", "import requests\nimport time\nfrom nav_msgs.msg import Path as ROSPath, Odometry\nfrom g1_task_voice import TaskVoice, register_voice_routes, validate_speech_steps\n")
    source = once(source, 'task_blueprint = Blueprint("task", __name__, url_prefix="/api/task")',
                  'task_blueprint = Blueprint("task", __name__, url_prefix="/api/task")\nregister_voice_routes(task_blueprint)')
    source = once(source, '        self.backend_post = rospy.get_param("/x_nav/backend_post", 9000)\n',
                  '        self.backend_post = rospy.get_param("/x_nav/backend_post", 9000)\n'
                  '        self.voice = TaskVoice("/opt/g1-arrival-voice/state", self.actual_path,\n'
                  '                               line_reach_meters=rospy.get_param("/x_nav/w_nav/reach_min_distance", 0.20))\n')
    source = once(source, "        rospy.Subscriber('/x_nav/slam/state', String, self.slam_state_callback)\n",
                  "        rospy.Subscriber('/x_nav/slam/state', String, self.slam_state_callback)\n"
                  "        rospy.Subscriber('/x_nav/goal_path', ROSPath, self._voice_goal_callback)\n"
                  "        rospy.Subscriber('/base_link/odom', Odometry, self._voice_pose_callback, queue_size=1)\n"
                  "        rospy.Subscriber('/planner/cmd', String, self._voice_control_callback, callback_args='/planner/cmd')\n"
                  "        rospy.Subscriber('/node_cmd', String, self._voice_control_callback, callback_args='/node_cmd')\n")
    source = once(source, '    def nav_state_callback1(self, msg):\n        if msg.data == "13":\n            self.reach_state = 1\n', '''    def _voice_record(self, msg, topic):
        header = getattr(msg, '_connection_header', None) or {}
        return {'received': time.monotonic(), 'topic': topic,
                'publisher': header.get('callerid'), 'latched': header.get('latching') == '1',
                'raw': getattr(msg, 'data', '')}

    def _voice_goal_callback(self, msg):
        record = self._voice_record(msg, '/x_nav/goal_path')
        record.update(header={'frame_id': msg.header.frame_id}, pose_count=len(msg.poses))
        if msg.poses:
            pose = msg.poses[-1].pose
            record['end_pose'] = {'position': [pose.position.x, pose.position.y, pose.position.z],
                                  'orientation': [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w]}
        self.voice.observe(record)

    def _voice_pose_callback(self, msg):
        record = self._voice_record(msg, '/base_link/odom')
        pose = msg.pose.pose.position
        record.update(frame_id=msg.header.frame_id,
                      position=[pose.x, pose.y, pose.z],
                      stamp_age=(rospy.Time.now() - msg.header.stamp).to_sec())
        self.voice.observe(record)

    def _voice_control_callback(self, msg, topic):
        self.voice.observe(self._voice_record(msg, topic))

    def nav_state_callback1(self, msg):
        record = self._voice_record(msg, '/x_nav/state')
        with self.voice.callback_lock:
            self._voice_nav_state_callback(msg, record)

    def _voice_nav_state_callback(self, msg, record):
        self.voice.observe(record)
        if self.voice.managed:
            try:
                voice_nav_state = self.voice.navigation_state()
            except Exception as error:
                self.reset_task(f"任务导航点核验失败，任务已停止: {error}")
                return
            if voice_nav_state == 'failed':
                self.reset_task("任务导航到达证据失效，任务已停止，请检查定位或重新开始任务")
                return
            if voice_nav_state == 'arrived' and self.task_reach_state == 1:
                self.reach_state = 1
        elif msg.data == "13":
            self.reach_state = 1
''')
    for command in ('multi_nav', 'nav_line'):
        before = '''            self.reach_state = 0
            self.service_manager.call_nav_function_service_cmd_string(
                "/x_nav/planner/service", "COMMAND", str(current_action_param)
            )
'''.replace('COMMAND', command)
        after = '''            self.reach_state = 0
            voice_run_before = self.voice.run_id
            voice_step_before = self.task_index
            try:
                self.voice.begin_navigation(get_current_map_name(), current_action_param)
                voice_plan_result = self.service_manager.call_nav_function_service_cmd_string(
                    "/x_nav/planner/service", "COMMAND", str(current_action_param)
                )
                if self.voice.managed and not voice_plan_result:
                    raise RuntimeError("路径规划请求失败")
            except Exception as error:
                self.reset_task(f"任务导航失败，任务已停止: {error}")
                return
            if self.voice.run_id != voice_run_before or self.task_index != voice_step_before:
                return
'''.replace('COMMAND', command)
        if command == 'nav_line':
            after = after.replace('current_action_param)\n', "current_action_param, mode='line')\n", 1)
        source = once(source, before, after)
    source = once(source, '''        elif current_action == "J":
            # 语音指令
            pass
''', '''        elif current_action == "J":
            voice_run_before = self.voice.run_id
            voice_step_before = self.task_index
            try:
                voice_result = self.voice.step(self.current_loop, self.task_index, current_action_param)
            except Exception as error:
                self.reset_task(f"语音步骤无效，任务已停止: {error}")
                return
            if self.voice.run_id != voice_run_before or self.task_index != voice_step_before:
                return
            if voice_result['state'] == 'submitted':
                self.task_index += 1
                self.time_index = 0
                self.task_state_str = f"语音请求已提交 任务进度: {self.task_index}/{len(self.task_buf)}"
            elif voice_result['state'] == 'failed':
                self.reset_task(f"语音播报失败，任务已停止: {voice_result.get('error', '未知结果，不自动重试')}")
            else:
                self.task_state_str = f"语音处理中 任务进度: {self.task_index}/{len(self.task_buf)}"
''')
    source = once(source, '        """重置任务状态"""\n', '        """重置任务状态"""\n        self.voice.cancel()\n        self.reach_state = 0\n        self.task_reach_state = 0\n')
    source = once(source, '        """创建一个新任务"""\n', '        """创建一个新任务"""\n        validate_speech_steps(data)\n')
    source = once(source, '        """更新指定索引的任务"""\n', '        """更新指定索引的任务"""\n        validate_speech_steps(data)\n')
    source = once(source, '        # 手动执行优先级最高，终止当前任务\n',
                  '        try:\n            voice_managed = validate_speech_steps(self.all_task[run_id])\n'
                  '        except ValueError as error:\n            self.task_state_str = f"任务语音配置无效: {error}"\n            return\n'
                  '        # 手动执行优先级最高，终止当前任务\n')
    source = once(source, '        task_obj = self.all_task[run_id]\n',
                  '        self.voice.start(voice_managed)\n        self.reach_state = 0\n        task_obj = self.all_task[run_id]\n')
    source = once(source, '                param = target.get("speechText", -1)\n', '                param = dict(target)\n')
    source = once(source, '    task_manager.paused = True\n', '    task_manager.voice.pause()\n    task_manager.paused = True\n')
    source = once(source, '    task_manager.paused = False\n', '''    task_manager.voice.resume()
    if task_manager.voice.managed and task_manager.task_reach_state == 1:
        # Pause invalidated arrival evidence; resume explicitly starts fresh navigation.
        task_manager.reach_state = 0
        task_manager.task_reach_state = 0
    task_manager.paused = False
''')
    # Pause speech before the original navigation pause request, not after its response.
    source = once(source, '    requests.get(f"http://127.0.0.1:{task_manager.backend_post}/api/extra/nav_work/cancel?stop=1")\n    task_manager.voice.pause()\n',
                  '    task_manager.voice.pause()\n    requests.get(f"http://127.0.0.1:{task_manager.backend_post}/api/extra/nav_work/cancel?stop=1")\n')
    for call in ('task_manager.create_task(json.loads(data[\'task_detail\']))', 'task_manager.update_task(update_id, task)'):
        source = once(source, '    ' + call + '\n', '    try:\n        ' + call + '\n    except ValueError as error:\n        return jsonify({"success": False, "msg": str(error)}), 400\n')
    ast.parse(source, feature_version=(3, 8))
    return source


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output')
    args = parser.parse_args()
    original = Path(args.input).read_text(encoding='utf-8')
    result = patch_task(original)
    Path(args.output).write_text(result, encoding='utf-8')
    print('input sha256:', hashlib.sha256(original.encode()).hexdigest())
    print('output sha256:', hashlib.sha256(result.encode()).hexdigest())
