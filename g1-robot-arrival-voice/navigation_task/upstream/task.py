import json
import traceback
from datetime import datetime, timedelta, timezone

import rospy
from flask import Blueprint, jsonify, request, current_app
import os
# from extra import  parse_file, ensure_file_exists
from std_msgs.msg import String
# import re
from extra import get_current_map_name
import requests

from croniter import croniter  # 必须安装: pip install croniter

task_blueprint = Blueprint("task", __name__, url_prefix="/api/task")

ROBOT_TASK_ACTIONS = {
    "A": ("cmd1", "进入模式1"),
    "B": ("cmd2", "进入模式2"),
    "C": ("cmd3", "卧倒"),
    "D": ("cmd4", "站立"),
    "M": ("cmd5", "执行指令1"),
    "N": ("cmd6", "执行指令2"),
    "O": ("cmd7", "执行指令3"),
    "P": ("cmd8", "执行指令4"),
}

GPS_TASK_ENDPOINTS = {
    "Q": ("gps_navigate", "RTK导航"),
    "R": ("gps_line_navigate", "RTK直线导航"),
    "S": ("reset_gps_location", "RTK重定位"),
}


def get_gps_point_index(point_name):
    """按名称查找 GPS 点位的接口索引，避免点位顺序调整后执行到错误目标。"""
    if not point_name:
        return -1
    rtk_file_path = "/nav_map/rtk_point.txt"
    if not os.path.exists(rtk_file_path):
        return -1
    with open(rtk_file_path, "r", encoding="utf-8") as file:
        for index, line in enumerate(file, start=1):
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0] == point_name:
                return index
    return -1


def parse_field(field, min_val, max_val):
    if field == "*":
        return list(range(min_val, max_val + 1))
    if field.startswith("*/"):
        step = int(field[2:])
        return list(range(min_val, max_val + 1, step))

    values = set()
    for part in field.split(","):
        if "-" in part:
            start, end = map(int, part.split("-"))
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    return sorted(values)


def format_time_simple(hour, minute):
    return f"{hour:02d}:{minute:02d}"


def format_time_description(hours, minutes):
    """格式化时间描述，支持范围、间隔和多个时间点"""

    # 检测是否是间隔模式
    def is_interval(values, start, end):
        """检查是否是等间隔序列"""
        if len(values) < 2:
            return False, 0
        diff = values[1] - values[0]
        for i in range(2, len(values)):
            if values[i] - values[i - 1] != diff:
                return False, 0
        # 确保从start开始
        if values[0] == start and values[-1] + diff > end:
            return True, diff
        return False, 0

    # 检测是否是连续范围
    def is_continuous_range(values):
        """检查是否是连续的数字序列"""
        if len(values) < 2:
            return False
        for i in range(1, len(values)):
            if values[i] - values[i - 1] != 1:
                return False
        return True

    # 特殊情况：多个小时配合单个固定分钟，组合成完整时间点
    # 例如：30 9,12,15 * * * → "9点30分、12点30分、15点30分"
    if 1 < len(hours) <= 5 and len(minutes) == 1 and not is_continuous_range(hours):
        if minutes[0] == 0:
            # 整点的情况
            return "、".join(f"{h}点" for h in hours)
        else:
            # 带分钟的情况
            return "、".join(f"{h}点{minutes[0]}分" for h in hours)

    time_parts = []

    # 处理小时
    if len(hours) == 24:  # 所有小时
        hour_desc = ""
    elif len(hours) == 1:  # 单个小时
        hour_desc = f"{hours[0]}点"
    else:
        # 检查是否是间隔
        is_hour_interval, hour_step = is_interval(hours, 0, 23)
        if is_hour_interval and hours[0] == 0:
            hour_desc = f"每{hour_step}小时"
        # 检查是否是连续范围
        elif is_continuous_range(hours):
            hour_desc = f"{hours[0]}点到{hours[-1]}点"
        # 多个不连续的小时
        elif len(hours) <= 5:
            hour_desc = "、".join(f"{h}点" for h in hours)
        else:
            hour_desc = f"{hours[0]}点到{hours[-1]}点等{len(hours)}个时间"

    # 处理分钟
    if len(minutes) == 60:  # 所有分钟
        minute_desc = "每分钟"
    elif len(minutes) == 1 and minutes[0] == 0:  # 整点
        minute_desc = ""
    elif len(minutes) == 1:  # 单个分钟
        minute_desc = f"{minutes[0]}分"
    else:
        # 检查是否是间隔
        is_minute_interval, minute_step = is_interval(minutes, 0, 59)
        if is_minute_interval and minutes[0] == 0:
            minute_desc = f"每{minute_step}分钟"
        # 检查是否是连续范围
        elif is_continuous_range(minutes):
            minute_desc = f"{minutes[0]}分到{minutes[-1]}分"
        # 多个不连续的分钟
        elif len(minutes) <= 5:
            minute_desc = "、".join(f"{m}分" for m in minutes)
        else:
            minute_desc = f"{minutes[0]}分到{minutes[-1]}分等{len(minutes)}个时间"

    # 组合时间描述
    if hour_desc and minute_desc:
        # 特殊处理：如果小时是范围且分钟是整点，表示"每小时"
        if "点到" in hour_desc and minute_desc == "":
            return f"{hour_desc}每小时"
        # 特殊处理：如果是间隔小时且分钟是整点
        if "每" in hour_desc and "小时" in hour_desc and minute_desc == "":
            return hour_desc
        return f"{hour_desc}{minute_desc}"
    elif hour_desc:
        if "每" in hour_desc and "小时" in hour_desc:
            return hour_desc
        return hour_desc if minute_desc == "" and len(minutes) == 1 and minutes[0] == 0 else hour_desc
    elif minute_desc:
        return minute_desc
    else:
        return format_time_simple(hours[0], minutes[0])


def weekdays_to_chinese(weekdays):
    WEEKDAY_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
    names = [WEEKDAY_NAMES[d] for d in sorted(weekdays)]
    if set(weekdays) == {1, 2, 3, 4, 5}:
        return "工作日"
    if len(names) == 2 and set(weekdays) == {6, 0}:
        return "周六、周日"
    if len(names) == 1:
        return names[0]
    return "、".join(names)


def months_to_chinese(months):
    if months == list(range(1, 13)):
        return ""
    return "、".join(f"{m}月" for m in months)


def cron_to_chinese(cron: str) -> str:
    parts = cron.strip().split()
    if len(parts) != 5:
        return "无效的 Cron 表达式"

    try:
        minutes = parse_field(parts[0], 0, 59)
        hours = parse_field(parts[1], 0, 23)
        days = parse_field(parts[2], 1, 31)
        months = parse_field(parts[3], 1, 12)
        weekdays = parse_field(parts[4], 0, 6)

        full_days = list(range(1, 32))
        full_weekdays = list(range(0, 7))
        full_months = list(range(1, 13))

        # 时间描述（支持范围、间隔和多个时间点）
        time_str = format_time_description(hours, minutes)

        # === 按优先级判断 ===

        # 1. 年度固定日（日固定 + 月固定 + 星期=*）
        if days != full_days and months != full_months and weekdays == full_weekdays:
            if len(days) == 1 and len(months) == 1:
                return f"每年 {months[0]} 月 {days[0]} 号 {time_str}"
            elif len(months) == 1:
                day_str = "、".join(str(d) for d in days[:3]) + ("..." if len(days) > 3 else "")
                return f"每年 {months[0]} 月 {day_str} 号 {time_str}"
            else:
                month_str = "、".join(f"{m}月" for m in months)
                day_str = "、".join(str(d) for d in days[:3]) + ("..." if len(days) > 3 else "")
                return f"每年 {month_str} 的 {day_str} 号 {time_str}"

        # 2. 月度固定日（日固定 + 月=* + 星期=*）
        if days != full_days and months == full_months and weekdays == full_weekdays:
            if len(days) == 1:
                return f"每月 {days[0]} 号 {time_str}"
            else:
                day_str = "、".join(str(d) for d in days[:3]) + ("..." if len(days) > 3 else "")
                return f"每月 {day_str} 号 {time_str}"

        # 3. 每天（日=* + 星期=*）
        if days == full_days and weekdays == full_weekdays:
            month_desc = months_to_chinese(months)
            if month_desc:
                return f"每年 {month_desc} 的 每天 {time_str}"
            else:
                return f"每天 {time_str}"

        # 4. 每周几（日=* + 星期≠*）
        if days == full_days and weekdays != full_weekdays:
            month_desc = months_to_chinese(months)
            day_desc = weekdays_to_chinese(weekdays)
            if month_desc:
                return f"每年 {month_desc} 的 每周{day_desc} {time_str}"
            else:
                return f"每周{day_desc} {time_str}"

        # 5. 其他复杂组合
        return f"复杂定时：{cron}"

    except Exception as e:
        return f"解析失败: {str(e)}"


class TaskServer:
    # def __init__(self, p_rosnode: ROSNode(), p_service: NavServiceManager(ROSNode())):
    def __init__(self, p_rosnode, p_service):

        self.ROSNode = p_rosnode
        self.service_manager = p_service
        self.all_task = []
        self.all_task_name = []
        self.task_state_str = "无任务"
        self.task_ = ""  # 当前任务
        self.current_task_name = ""
        self.slam_mode = 0  # 0 mapping 1 slam
        self.time_index = 0  # 时间索引
        self.task_index = 0  # 任务索引

        self.error_task_state = 0  # 没有触发异常
        self.error_task_index = 0  # 异常任务索引
        self.task_reach_state = 0  ## 0 未设置目标点  1已经设置目标点  2到达目标点
        self.task_buf = []  # 任务指令
        self.task_param_buf = []  # 任务参数
        self.task_error_buf = []  # 任务错误指令
        self.task_error_param_buf = []  # 任务错误参数

        self.actual_path = rospy.get_param("/x_nav/nav_map_path", "/nav_map")
        self.backend_post = rospy.get_param("/x_nav/backend_post", 9000)
        self.load_tasks()
        self.pre_map_name = ""  # 上一个地图
        self.pre_point = ""  # 上一个点
        self.pre_reach_point = ""  # 上一个点
        self.reach_state = 0

        # 循环执行相关
        self.loop_count = 1  # 循环次数
        self.current_loop = 0  # 当前已执行次数
        self.schedule_check_counter = 0  # 定时任务检查计数器
        self.paused = False  # 新增：是否处于暂停状态
        self.map_list = get_map_list()
        self.pub_cmd_planner = rospy.Publisher('/planner/cmd', String, queue_size=10)
        rospy.Subscriber('/x_nav/state', String, self.nav_state_callback1)
        rospy.Subscriber('/x_nav/slam/state', String, self.slam_state_callback)

    def nav_state_callback1(self, msg):
        if msg.data == "13":
            self.reach_state = 1
        if msg.data == "15" or self.error_task_state == 1:  # 接收到导航目标点，路径规划失败
            if msg.data == "15":
                self.error_task_state = 1  # 标记进入异常处理流程

            if self.task_buf:  # 清空任务
                self.reach_state = 0
                self.time_index = 0
                self.task_reach_state = 0
                self.task_index = 0
                self.error_task_index = 0
                self.task_buf.clear()
                self.task_param_buf.clear()

            if not self.task_error_buf:
                self.error_task_index = 0
                self.task_state_str = f"此任务无设置异常处理，将跳过并取消任务"
                self.reset_task("无任务")
                self.error_task_state = 0
                return

            if self.error_task_index >= len(self.task_error_buf):
                self.reset_task(f"执行任务{self.current_task_name}失败, 异常处理完成")
                self.error_task_state = 0
                return

            error_action = self.task_error_buf[self.error_task_index]
            error_action_param = self.task_error_param_buf[self.error_task_index]

            if error_action == "A":
                self.time_index = 0
                self.error_task_index += 1
                self.task_state_str = f"任务异常处理 进入模式1: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "B":
                self.time_index = 0
                self.error_task_index += 1
                self.task_state_str = f" 任务异常处理 进入模式2: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "C":
                self.time_index = 0
                self.error_task_index += 1
                self.task_state_str = f"任务异常处理 卧倒: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "D":
                self.time_index = 0
                self.error_task_index += 1
                self.task_state_str = f"任务异常处理 站立: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "E" and self.task_reach_state == 0:
                self.time_index = 0
                if error_action_param == 0:
                    error_action_param = self.pre_reach_point if self.pre_reach_point else 1
                self.service_manager.call_nav_function_service_cmd_string(
                    "/x_nav/planner/service", "multi_nav", str(error_action_param)
                )
                self.task_reach_state = 1
                self.task_state_str = f"任务异常处理 导航中: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "E" and self.reach_state == 1:
                self.reach_state = 0
                self.task_reach_state = 0
                self.error_task_index += 1
                self.time_index = 0
                self.task_state_str = f"任务异常处理 到达导航点: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "F" and self.time_index > error_action_param * 100:
                self.error_task_index += 1
                self.time_index = 0
                self.task_state_str = f"任务异常处理 完成等待: {self.error_task_index}/{len(self.task_error_buf)}"

            elif error_action == "F":
                self.task_state_str = f"任务异常处理 等待中: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "H":

                current_map = get_current_map_name()
                map_list = self.map_list
                if current_map not in map_list:
                    self.error_task_index += 1
                    self.time_index = 0
                    return
                map_id = map_list.index(current_map)
                url = f"http://127.0.0.1:{self.backend_post}/api/extra/switch_map"
                requests.post(url, json={"id": map_id + 1})
                requests.get(f"http://127.0.0.1:{self.backend_post}/api/extra/refresh_point_cloud")
                self.error_task_index += 1
                self.time_index = 0
                self.task_state_str = f"任务异常处理 切换地图至 {map_list[map_id]}: {self.task_index}/{len(self.task_buf)}"

            elif error_action == "I":
                # 重定位
                if self.pre_reach_point:
                    point_id = int(self.pre_reach_point)
                else:
                    point_id = 1
                    # point_id = int(error_action_param)
                url = f"http://127.0.0.1:{self.backend_post}/api/extra/reset_location"
                response = requests.post(url, json={"id": point_id})
                self.error_task_index += 1
                self.time_index = 0
                self.task_state_str = f"任务异常处理 执行重定位: {self.task_index}/{len(self.task_buf)}"


            elif error_action == "L" and self.task_reach_state == 0:
                self.time_index = 0
                if error_action_param == 0:
                    error_action_param = self.pre_reach_point if self.pre_reach_point else 1
                self.service_manager.call_nav_function_service_cmd_string(
                    "/x_nav/planner/service", "nav_line", str(error_action_param)
                )
                self.task_reach_state = 1
                self.task_state_str = f"任务异常处理 导航中: {self.error_task_index}/{len(self.task_error_buf)}"
            elif error_action == "L" and self.reach_state == 1:
                self.reach_state = 0
                self.task_reach_state = 0
                self.error_task_index += 1
                self.time_index = 0
                self.task_state_str = f"任务异常处理 到达导航点: {self.error_task_index}/{len(self.task_error_buf)}"

            return  # 异常处理流程中，不继续执行正常任务逻辑

        # 如果还没加载任务，不执行

        if not self.task_buf:
            self.task_state_str = "无任务"
            return
        if self.paused:
            self.task_state_str = f"任务 {self.current_task_name} 已暂停"
            return
        if self.task_index >= len(self.task_buf):
            self.current_loop += 1
            if self.current_loop < self.loop_count:
                # 还需要继续循环，重置任务索引
                self.task_index = 0
                self.time_index = 0
                self.task_reach_state = 0
                self.task_state_str = f"任务{self.current_task_name} 第{self.current_loop}次完成，准备第{self.current_loop + 1}次执行"
                return
            # 任务完成，标记已执行（如果是定时任务）
            self.reset_task(f"任务{self.current_task_name}已完成(共执行{self.current_loop}次)")
            return

        current_action = self.task_buf[self.task_index].upper()
        current_action_param = self.task_param_buf[self.task_index]
        # ========== 动作处理 ==========
        if current_action in ROBOT_TASK_ACTIONS:
            command, action_name = ROBOT_TASK_ACTIONS[current_action]
            msg = String()
            msg.data = command
            self.ROSNode.pub_cmd.publish(msg)
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"{action_name} 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "E" and self.task_reach_state == 0:
            if current_action_param == -1:
                # 找不到点位直接跳过
                self.task_index += 1
                self.time_index = 0
                return
            elif current_action_param == 0:
                current_action_param = self.pre_reach_point if self.pre_reach_point else 1
            self.pre_point = str(current_action_param)

            self.reach_state = 0
            self.service_manager.call_nav_function_service_cmd_string(
                "/x_nav/planner/service", "multi_nav", str(current_action_param)
            )
            self.time_index = 0
            self.task_reach_state = 1
            self.task_state_str = f"导航中 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "E" and self.reach_state == 1:
            self.reach_state = 0
            if self.pre_point.isdigit():
                self.pre_reach_point = str(self.pre_point)
            self.task_reach_state = 0
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"到达导航点 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "F" and self.time_index > current_action_param * 100:
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"完成等待 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "F":
            self.task_state_str = f"等待中 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "G":
            msg = String()
            msg.data = f"take_picture#{get_current_map_name()}"
            self.ROSNode.pub_cmd.publish(msg)
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"拍照 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "H":
            map_id = int(current_action_param)  # 或从原始 step 中取
            map_list = get_map_list()

            url = f"http://127.0.0.1:{self.backend_post}/api/extra/switch_map"
            response = requests.post(url, json={"id": map_id})
            requests.get(f"http://127.0.0.1:{self.backend_post}/api/extra/refresh_point_cloud")
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"切换地图至 {map_list[map_id - 1]} 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "I":
            # 重定位
            point_id = int(current_action_param)
            url = f"http://127.0.0.1:{self.backend_post}/api/extra/reset_location"
            response = requests.post(url, json={"id": point_id})
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"执行重定位 任务进度: {self.task_index}/{len(self.task_buf)}"
        elif current_action == "T":
            # 强制重定位
            point_id = int(current_action_param)
            url = f"http://127.0.0.1:{self.backend_post}/api/extra/force_relocation"
            requests.post(url, json={"id": point_id})
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"执行强制重定位 任务进度: {self.task_index}/{len(self.task_buf)}"
        elif current_action == "J":
            # 语音指令
            pass
        elif current_action == "K":
            # 自定义命令：纯数字参数按 get_commands 的全局序号查找，否则直接作为命令执行
            raw_param = str(current_action_param if current_action_param is not None else "").strip()
            command = raw_param
            command_source = "自定义命令"
            try:
                if raw_param.isdigit():
                    command_index = int(raw_param)
                    if command_index < 1:
                        raise ValueError("命令序号必须从 1 开始")

                    page = (command_index - 1) // 10 + 1
                    page_offset = (command_index - 1) % 10
                    commands_url = f"http://127.0.0.1:{self.backend_post}/api/system/get_commands"
                    commands_response = requests.get(commands_url, params={"page": page}, timeout=10)
                    commands_response.raise_for_status()
                    commands = commands_response.json().get("data", [])
                    if page_offset >= len(commands):
                        raise ValueError(f"命令序号 {command_index} 不存在")

                    command_item = commands[page_offset]
                    if not isinstance(command_item, dict):
                        raise ValueError(f"命令序号 {command_index} 的数据格式错误")
                    command = str(command_item.get("command", "")).strip()
                    command_source = f"命令序号 {command_index}"

                if not command:
                    raise ValueError("命令不能为空")

                execute_url = f"http://127.0.0.1:{self.backend_post}/api/system/execute_cmd"
                execute_response = requests.post(execute_url, json={"command": command})
                execute_response.raise_for_status()
                execute_result = execute_response.json()
                if not execute_result.get("success", False):
                    raise RuntimeError(execute_result.get("msg", "命令执行失败"))
                self.task_state_str = f"执行{command_source}完成 任务进度: {self.task_index + 1}/{len(self.task_buf)}"
            except (requests.RequestException, ValueError, RuntimeError) as error:
                self.task_state_str = f"执行自定义命令失败: {error}，已跳过 任务进度: {self.task_index + 1}/{len(self.task_buf)}"
            self.task_index += 1
            self.time_index = 0

        elif current_action in GPS_TASK_ENDPOINTS:
            endpoint, action_name = GPS_TASK_ENDPOINTS[current_action]
            point_id = int(current_action_param)
            if point_id > 0:
                url = f"http://127.0.0.1:{self.backend_post}/api/extra/{endpoint}"
                requests.post(url, json={"id": point_id})
                self.task_state_str = f"{action_name} 任务进度: {self.task_index + 1}/{len(self.task_buf)}"
            else:
                self.task_state_str = f"{action_name}点位不存在，已跳过 任务进度: {self.task_index + 1}/{len(self.task_buf)}"
            self.task_index += 1
            self.time_index = 0

        elif current_action == "L" and self.task_reach_state == 0:
            if current_action_param == -1:
                # 找不到点位直接跳过
                self.task_index += 1
                self.time_index = 0
                return
            elif current_action_param == 0:
                current_action_param = self.pre_reach_point if self.pre_reach_point else 1
            self.pre_point = str(current_action_param)

            self.reach_state = 0
            self.service_manager.call_nav_function_service_cmd_string(
                "/x_nav/planner/service", "nav_line", str(current_action_param)
            )
            self.time_index = 0
            self.task_reach_state = 1
            self.task_state_str = f"导航中 任务进度: {self.task_index}/{len(self.task_buf)}"

        elif current_action == "L" and self.reach_state == 1:
            self.reach_state = 0
            if self.pre_point.isdigit():
                self.pre_reach_point = str(self.pre_point)
            self.task_reach_state = 0
            self.task_index += 1
            self.time_index = 0
            self.task_state_str = f"到达导航点 任务进度: {self.task_index}/{len(self.task_buf)}"

        # else:
        #     # 未知动作，跳过

    def slam_state_callback(self, msg):
        parts = msg.data.split("#")

    def is_func(self):
        pass

    def load_tasks(self):
        """从 task.json 加载所有任务"""
        file_path = os.path.join(self.actual_path, "task.json")
        if not os.path.exists(file_path):
            print(f"未找到 {file_path}，正在创建...")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.all_task = json.load(f)
        except Exception as e:
            print(f"加载 task.json 失败: {e}")
            self.all_task = []

    def save_tasks(self):
        """保存所有任务到 task.json"""
        file_path = os.path.join(self.actual_path, "task.json")
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.all_task, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存 task.json 失败: {e}")

    def get_task_state(self):
        return self.task_state_str

    def reset_task(self, msg="", cancel_nav=False):
        """重置任务状态"""
        if cancel_nav:
            # ros_msg = String()
            # ros_msg.data = "cancel_nav"
            # self.pub_cmd_planner.publish(ros_msg)
            requests.get(f"http://127.0.0.1:{self.backend_post}/api/extra/cancel_nav")
        self.task_index = 0
        self.error_task_index = 0
        self.task_buf.clear()
        self.task_param_buf.clear()
        self.task_error_buf.clear()
        self.task_error_param_buf.clear()
        self.loop_count = 1
        self.current_loop = 0
        self.task_state_str = f"任务{self.current_task_name}取消"
        self.paused = False
        if msg:
            self.task_state_str = msg

    def get_task(self):
        """返回任务列表用于前端显示，包含下次执行时间（东八区）"""
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        scheduled_tasks = []  # 定时任务
        normal_tasks = []  # 普通任务
        tasks = []
        for i, task in enumerate(self.all_task):
            schedule_type = task.get("scheduleType", "")
            next_run = ""  # 下次执行时间（东八区，用于前端显示）

            if schedule_type == "datetime":
                execute_time_str = task.get("executeTime", "")  # 东八区时间
                executed = task.get("executed", False)
                if execute_time_str and not executed:
                    next_run = execute_time_str  # 直接返回东八区时间给前端

            elif schedule_type == "cron":
                cron_expr = task.get("cronExpression", "")
                if cron_expr:
                    try:
                        # cron表达式基于东八区
                        now_local = now_utc + timedelta(hours=8)
                        last_run_str = task.get("lastRunTime", "")
                        base_time = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S") if last_run_str else now_local
                        cron = croniter(cron_expr, base_time)
                        next_run_dt = cron.get_next(datetime)
                        # 已经是东八区时间
                        next_run = next_run_dt.strftime("%Y-%m-%d %H:%M:%S")
                    except:
                        next_run = "表达式错误"

            item = {
                "col1": task.get("taskName", f"任务{i}"),
                "col2": "执行",
                "col3": "编辑",
                "col4": "删除",
                "nextRun": next_run,  # 下次执行时间（东八区）
                "originalIndex": i,  # 原始索引，用于排序和操作
            }

            # if next_run and next_run != "表达式错误":
            #     scheduled_tasks.append(item)
            # else:
            #     normal_tasks.append(item)
            tasks.append(item)
        # 定时任务按执行时间升序（最近的排前面）
        # scheduled_tasks.sort(key=lambda x: x["nextRun"])
        # # 普通任务按添加顺序倒序（最新添加的排前面）
        # normal_tasks.reverse()

        # 定时任务在前，普通任务在后
        # return scheduled_tasks + normal_tasks
        return tasks

    def create_task(self, data):
        """创建一个新任务"""
        if not data.get("taskName"):
            raise ValueError("任务名不能为空")
        self.all_task.append(data)
        self.save_tasks()

    def get_detail_task(self, index):
        """返回指定索引的任务详情"""
        if index < 0 or index >= len(self.all_task):
            raise IndexError("任务索引超出范围")
        return self.all_task[index]

    def update_task(self, index, data):
        """更新指定索引的任务"""
        if index < 0 or index >= len(self.all_task):
            raise IndexError("任务索引超出范围")
        self.all_task[index] = data
        self.save_tasks()

    def delete_task(self, index):
        """删除指定索引的任务"""
        if index < 0 or index >= len(self.all_task):
            return 0
        del self.all_task[index]
        self.save_tasks()
        return 1

    def do_task(self, run_id, is_scheduled=False):
        """准备执行任务（加载到当前任务缓冲区）
        is_scheduled: 是否由定时调度触发

        注意：手动执行优先级最高，会终止当前正在执行的任务
        """
        self.paused = False
        if run_id < 0 or run_id >= len(self.all_task):
            self.task_state_str = "任务不存在"
            return

        # 手动执行优先级最高，终止当前任务
        if not is_scheduled and self.task_buf:
            print(f"手动执行任务，终止当前任务: {self.current_task_name}")
            self.reset_task(f"任务{self.current_task_name}被手动中断", cancel_nav=True)

        task_obj = self.all_task[run_id]
        self.current_task_name = task_obj.get("taskName", "未知任务")

        # 获取循环次数，默认循环1次
        self.loop_count = int(task_obj.get("loopCount", 1))
        self.current_loop = 0

        # 合并 beforeSteps + subTasks
        # 合并 beforeSteps + subTasks + afterSteps
        full_steps = []

        # 添加 defaults.beforeSteps
        defaults = task_obj.get("defaults", {})
        before_steps = defaults.get("beforeSteps", [])
        full_steps.extend(before_steps)

        # 添加 subTasks
        sub_tasks = task_obj.get("subTasks", [])
        full_steps.extend(sub_tasks)

        # 添加 afterSteps
        after_steps = defaults.get("afterSteps", [])
        full_steps.extend(after_steps)

        # 添加errorSteps
        error_steps = defaults.get("errorSteps", [])
        self.task_error_buf.clear()
        self.task_error_param_buf.clear()
        for step in error_steps:
            action = step.get("action", "")
            if action == "E0":  # 回到上一点
                action = "E"
                param = 0  # 执行到此步骤时才可获取到上一点是多少
            elif action == "E1":  # 回到原点
                action = "E"
                param = 1  # 当前地图原点就是1
            elif action == "H0":
                action = "H"
                param = 0
            elif action == "I0":
                action = "I"
                param = 0
            else:
                param = -1
            self.task_error_buf.append(action)
            self.task_error_param_buf.append(param)
            wait = str(step.get("wait", 0))
            wait = int(wait) if wait.isdigit() else 0
            if wait > 0:
                wait_action = "F"
                self.task_error_buf.append(wait_action)
                self.task_error_param_buf.append(wait)
        self.task_buf.clear()
        self.task_param_buf.clear()
        map_list = get_map_list()
        self.map_list = map_list
        for step in full_steps:

            action = step.get("action", "")
            target = step.get("target", {})
            print("action", action)
            if action == "E":
                param = target.get("waypointIndex", -1)
            elif action == "E0":  # 回到上一点
                action = "E"
                param = 0  # 执行到此步骤时才可获取到上一点是多少
            elif action == "E1":  # 回到原点
                action = "E"
                param = 1  # 当前地图原点就是1
            elif action in ["H"]:
                name_param = target.get("map", "")
                if name_param in map_list:
                    param = map_list.index(name_param) + 1
                else:
                    param = target.get("mapIndex", -1)
            elif action == "H0":
                action = "H"
                param = 0  # 执行到此步骤时才可获取到当前地图是多少
            elif action in ["I", "T"]:
                param = target.get("waypointIndex", -1)
            elif action == "I0":
                action = "I"
                param = 0  # 执行到此步骤时才可获取到当前点位是多少，没找到就回原点
            elif action == "J":
                param = target.get("speechText", -1)
            elif action == "K":
                param = target.get("arg", -1)
            elif action == "L":
                param = target.get("waypointIndex", -1)
            elif action in GPS_TASK_ENDPOINTS:
                param = get_gps_point_index(target.get("gpsPoint", ""))
                if param == -1:
                    param = target.get("gpsPointIndex", -1)
            else:
                param = -1
            self.task_buf.append(action)
            self.task_param_buf.append(param)
            wait = str(step.get("wait", 0))
            wait = int(wait) if wait.isdigit() else 0
            if wait > 0:
                wait_action = "F"
                self.task_buf.append(wait_action)
                self.task_param_buf.append(wait)

        self.task_index = 0
        self.time_index = 0
        self.task_reach_state = 0

        schedule_info = "(定时)" if is_scheduled else ""
        loop_info = f"(循环{self.loop_count}次)" if self.loop_count > 1 else ""
        self.task_state_str = f"准备执行任务: {self.current_task_name}{schedule_info}{loop_info}"
        if get_current_map_name().strip() == "":
            valid_maps = [m for m in map_list if m != "__pycache__"]
            if valid_maps:
                requests.post(f"http://127.0.0.1:{self.backend_post}/api/extra/switch_map",
                              json={"id": map_list.index(valid_maps[0]) + 1})

    def run_task_time(self):  # 100hz
        if self.paused:
            return
        self.time_index += 1
        # 每秒检查一次定时任务（100hz / 100 = 1秒）
        self.schedule_check_counter += 1
        if self.schedule_check_counter >= 100:
            self.schedule_check_counter = 0
            self.check_scheduled_tasks()

    def check_scheduled_tasks(self):
        """检查是否有定时任务需要执行
        支持两种模式：
        1. executeTime: 具体时间（东八区），精确到分钟匹配
        2. cronExpression: cron表达式，周期性执行

        规则：
        - 开始执行就标记已执行
        - 超过执行时间的任务不再执行
        - 多个定时任务冲突时只执行一个
        """
        # 当前有任务在执行，不检查
        if self.task_buf or self.task_error_buf:
            return

        # 使用UTC时间（推荐写法，utcnow已弃用）
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        now_minute = now_utc.replace(second=0, microsecond=0)

        for i, task in enumerate(self.all_task):
            task_name = task.get("taskName", f"任务{i}")
            schedule_type = task.get("scheduleType", "")  # "datetime" 或 "cron"
            # ========== 模式1: 具体时间（一次性执行）==========
            if schedule_type == "datetime":
                execute_time_str = task.get("executeTime", "")  # 东八区时间
                executed = task.get("executed", False)
                if not execute_time_str or executed:
                    continue

                try:
                    # 解析东八区时间
                    if len(execute_time_str) == 16:
                        execute_time_local = datetime.strptime(execute_time_str, "%Y-%m-%d %H:%M")
                    else:
                        execute_time_local = datetime.strptime(execute_time_str, "%Y-%m-%d %H:%M:%S")

                    # 东八区转UTC（减8小时）
                    execute_time_utc = execute_time_local - timedelta(hours=8)
                    execute_minute_utc = execute_time_utc.replace(second=0, microsecond=0)
                    # 精确匹配：当前分钟 == 设定时间的分钟
                    if now_minute == execute_minute_utc:
                        print(f"定时任务触发(具体时间): {now_minute}, 设定时间: {execute_time_str}")
                        # 开始执行前就标记已执行
                        self.all_task[i]["executed"] = True
                        self.save_tasks()
                        self.do_task(i, is_scheduled=True)
                        return  # 一次只执行一个任务

                except ValueError as e:
                    print(f"任务 {task_name} 的执行时间格式错误: {execute_time_str}, {e}")
                except Exception as e:
                    print(f"任务 {task_name} 错误: {e}")

            # ========== 模式2: Cron表达式（周期执行，基于东八区）==========
            elif schedule_type == "cron":
                cron_expr = task.get("cronExpression", "")
                if not cron_expr:
                    continue

                try:
                    # cron表达式基于东八区，所以用东八区时间计算
                    now_local = now_utc + timedelta(hours=8)
                    now_local_minute = now_local.replace(second=0, microsecond=0)

                    # 获取上次执行时间（东八区）
                    last_run_str = task.get("lastRunTime", "")
                    if last_run_str:
                        last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
                    else:
                        # 首次执行，设置为1分钟前
                        last_run = now_local - timedelta(minutes=1)

                    # 计算下一次执行时间（东八区）
                    cron = croniter(cron_expr, last_run)
                    next_run = cron.get_next(datetime)
                    next_run_minute = next_run.replace(second=0, microsecond=0)

                    # 精确匹配：当前东八区分钟 == 下次执行时间的分钟
                    if next_run_minute == now_local_minute:
                        print(f"定时任务触发(Cron): {task_name}, 表达式: {cron_expr}")
                        # 开始执行前就更新上次执行时间（东八区）
                        self.all_task[i]["lastRunTime"] = now_local.strftime("%Y-%m-%d %H:%M:%S")
                        self.save_tasks()
                        self.do_task(i, is_scheduled=True)
                        return  # 一次只执行一个任务

                except Exception as e:
                    print(f"任务 {task_name} cron表达式错误: {cron_expr}, {e}")

    def run_task(self):
        # global reach_state

        a = 0


def get_map_list():
    # ROSNode = current_app.config['ROSNode']
    # actual_path = ROSNode.actual_path
    actual_path = rospy.get_param("/x_nav/nav_map_path", "/nav_map")
    if not os.path.exists(actual_path):
        return []
    map_list = [name for name in os.listdir(actual_path) if os.path.isdir(os.path.join(actual_path, name))]

    map_list.sort(key=lambda x: os.path.getctime(os.path.join(actual_path, x)), reverse=True)
    return map_list


def parse_nav_data(file_path):
    nav_data = []

    with open(file_path, 'r') as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 11:
                nav_data.append(parts[-1])
    return nav_data


def ensure_file_exists(file_path, default_line):
    """如果文件不存在，则创建并写入默认行"""
    if not os.path.exists(file_path):
        rospy.loginfo("File does not exist. Creating new file.")
        with open(file_path, 'w') as f:
            f.write(default_line + '\n')
        rospy.loginfo("Default line written to the file.")


def get_map_nav_data(map_name):
    # ROSNode = current_app.config['ROSNode']
    # actual_path = ROSNode.actual_path
    actual_path = rospy.get_param("/x_nav/nav_map_path", "/nav_map")
    current_map_path = os.path.join(actual_path, map_name)

    nav_file_path = os.path.join(current_map_path, "nav_points.txt")
    default_first_line = "0 0 0 0 0 0 1 0 0 0 原点"
    ensure_file_exists(nav_file_path, default_first_line)
    nav_data = parse_nav_data(nav_file_path)
    return nav_data


def parse_position_data(file_path):
    position_data = []

    with open(file_path, 'r') as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            # floats = list(map(float, parts[:3]))
            # x y z qx qy qz qw（x,y,z和四元数）
            floats = tuple(map(float, parts[:7]))
            position_data.append(floats)
    return position_data


def get_point_positions(map_name):
    actual_path = rospy.get_param("/x_nav/nav_map_path", "/nav_map")
    current_map_path = os.path.join(actual_path, map_name)

    nav_file_path = os.path.join(current_map_path, "nav_points.txt")
    default_first_line = "0 0 0 0 0 0 1 0 0 0 原点"
    ensure_file_exists(nav_file_path, default_first_line)
    nav_data = parse_position_data(nav_file_path)
    return nav_data


# 模块级导航数据缓存：{ map_name: {"points": [...], "positions": [...]} }
_nav_data_cache = {}


@task_blueprint.route('/create_task', methods=['POST'])
def create_task():
    data = request.get_json()
    print("任务详情:", data['task_detail'])
    task_manager = current_app.config['task_manager']

    task_manager.create_task(json.loads(data['task_detail']))

    return jsonify(
        {
            "success": True,
            "msg": "新建任务成功",
        }
    )


@task_blueprint.route('/get_task_data', methods=['GET'])
def get_task_data():
    page = int(request.args.get('page', 1))
    # 示例数据
    print("获取任务列表,页码:", page)
    task_manager = current_app.config['task_manager']
    data = task_manager.get_task()

    total_pages = (len(data) + 10 - 1) // 10
    return jsonify({"data": data[(page - 1) * 10:page * 10], "totalPages": total_pages})


@task_blueprint.route('/delete_task_data', methods=['POST'])
def delete_task_data():
    data = request.get_json()
    delete_id = data['id'] - 1

    task_manager = current_app.config['task_manager']
    state = task_manager.delete_task(delete_id)

    if state == 1:
        return jsonify(
            {
                "success": True,
                "msg": "删除成功",
            }
        )
    else:
        return jsonify(
            {
                "success": False,
                "msg": "删除失败",
            }
        )


@task_blueprint.route('/do_task', methods=['POST'])
def do_task():
    data = request.get_json()
    print("任务id:", data['id'])
    run_id = data['id'] - 1

    task_manager = current_app.config['task_manager']
    task_manager.do_task(run_id)

    return jsonify(
        {
            "success": True,
            "msg": "执行成功",
        }
    )


@task_blueprint.route('/update_task_data', methods=['POST'])
def update_task_data():
    data = request.get_json()
    print("编辑任务id:", data['id'])
    print("参数:", data['task_param'])
    update_id = data['id'] - 1
    task = json.loads(data['task_param'])
    task_manager = current_app.config['task_manager']
    task_manager.update_task(update_id, task)

    return jsonify(
        {
            "success": True,
            "msg": "编辑成功",
        }
    )


def _load_map_nav_cache(map_name, refresh=False):
    """加载指定地图的导航数据到缓存，refresh=True 时强制重新读取文件"""
    if refresh or map_name not in _nav_data_cache:
        _nav_data_cache[map_name] = {
            "points": get_map_nav_data(map_name),
            "positions": get_point_positions(map_name),
        }
    return _nav_data_cache[map_name]


@task_blueprint.route("/get_all_map_nav_data", methods=["GET"])
def get_all_map_nav_data():
    """
    GET /api/task/get_all_map_nav_data
    查询参数：
      map       （可选）地图名称；传入时返回该地图所有点的详细坐标信息
      refresh   （可选，默认 false）为 true 时忽略缓存、重新读取文件
      page      （可选）页码（从 1 开始）；不传则返回全部数据
      page_size （可选，默认 50）每页条数；仅在传入 page 时生效
    """
    map_param = request.args.get("map", None)
    refresh = request.args.get("refresh", "false").lower() == "true"
    page_str = request.args.get("page", None)
    page_size = int(request.args.get("page_size", 50))

    def paginate(data):
        """对列表做可选分页，不传 page 则返回全部"""
        total = len(data)
        if page_str is None:
            return data, total, 1, 1
        page = max(1, int(page_str))
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return data[start:start + page_size], total, page, total_pages

    if map_param:
        # 新模式：返回指定地图每个点的名称 + 完整坐标
        try:
            cached = _load_map_nav_cache(map_param, refresh=refresh)
        except Exception as e:
            return jsonify({"success": False, "msg": f"读取地图 {map_param} 数据失败: {e}"})

        point_detail = []
        for i, (name, pos) in enumerate(zip(cached["points"], cached["positions"])):
            point_detail.append({
                "index": i + 1,
                "name": name,
                "x": pos[0], "y": pos[1], "z": pos[2],
                "qx": pos[3], "qy": pos[4], "qz": pos[5], "qw": pos[6],
            })

        paged_data, total, page, total_pages = paginate(point_detail)
        return jsonify({
            "data": paged_data,
            "map": map_param,
            "current_map_name": get_current_map_name(),
            "total": total,
            "page": page,
            "page_size": page_size if page_str else total,
            "total_pages": total_pages,
        })

    # 原模式（兼容旧逻辑）：返回所有地图及其点名列表
    map_list = get_map_list()
    nav_data = []
    for map_name in map_list:
        cached = _load_map_nav_cache(map_name, refresh=refresh)
        nav_data.append({"name": map_name, "points": cached["points"]})

    paged_data, total, page, total_pages = paginate(nav_data)
    return jsonify({
        "data": paged_data,
        "current_map_name": get_current_map_name(),
        "total": total,
        "page": page,
        "page_size": page_size if page_str else total,
        "total_pages": total_pages,
    })


@task_blueprint.route("/get_detail_task", methods=["GET"])
def get_detail_task():
    task_id = int(request.args.get("id")) - 1
    task_manager = current_app.config['task_manager']
    data = task_manager.get_detail_task(task_id)
    return jsonify({"data": data})


@task_blueprint.route("/stop_task", methods=["GET"])
def stop_task():
    task_manager = current_app.config['task_manager']
    task_manager.reset_task(cancel_nav=True)
    return jsonify(
        {
            "success": True,
            "msg": "操作成功",
        }
    )


@task_blueprint.route("/pause_task", methods=["GET"])
def pause_task():
    task_manager = current_app.config['task_manager']
    if not task_manager.task_buf:
        return jsonify({"success": False, "msg": "当前无任务可暂停"})
    if task_manager.paused:
        return jsonify({"success": False, "msg": "任务已处于暂停状态"})
    # msg = String()
    # msg.data = f"disable_localplanner"
    # task_manager.pub_cmd_planner.publish(msg)
    requests.get(f"http://127.0.0.1:{task_manager.backend_post}/api/extra/nav_work/cancel?stop=1")
    task_manager.paused = True
    task_manager.task_state_str = f"任务 {task_manager.current_task_name} 已暂停"
    return jsonify({"success": True, "msg": "任务已暂停"})


@task_blueprint.route("/resume_task", methods=["GET"])
def resume_task():
    task_manager = current_app.config['task_manager']
    if not task_manager.task_buf:
        return jsonify({"success": False, "msg": "当前无任务可恢复"})
    if not task_manager.paused:
        return jsonify({"success": False, "msg": "任务未处于暂停状态"})
    # msg = String()
    # msg.data = f"enable_localplanner"
    # task_manager.pub_cmd_planner.publish(msg)
    requests.get(f"http://127.0.0.1:{task_manager.backend_post}/api/extra/nav_work/cancel")
    task_manager.paused = False
    task_manager.task_state_str = f"任务 {task_manager.current_task_name} 已恢复"
    return jsonify({"success": True, "msg": "任务已恢复"})


@task_blueprint.route("/parse_cron", methods=["POST"])
def parse_cron():
    """解析cron表达式，返回中文描述"""
    data = request.get_json()
    cron_expr = data.get("cron", "")

    if not cron_expr:
        return jsonify({"success": False, "msg": "cron表达式不能为空"})

    try:
        desc = cron_to_chinese(cron_expr)
        return jsonify({"success": True, "description": desc})
    except Exception as e:
        return jsonify({"success": False, "msg": f"无效的Cron表达式: {str(e)}"})
