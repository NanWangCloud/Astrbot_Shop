import os
import json
import asyncio
import aiohttp
import qrcode
import random
import string
import hashlib
from io import BytesIO
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import session_waiter, SessionController

# 数据模型
@dataclass
class Product:
    id: str
    name: str
    price: float
    quantity: int
    delivery_type: str  # auto, manual
    description: str
    status: str = "active"

@dataclass
class Order:
    order_no: str
    user_id: str
    product_id: str
    product_name: str
    quantity: int
    amount: float
    status: str  # pending, paid, delivered, cancelled, expired
    delivery_type: str
    user_email: str
    payment_url: str = ""
    qr_code_path: str = ""
    expire_time: datetime = None
    created_at: datetime = None
    paid_at: datetime = None

@dataclass
class UserEmail:
    user_id: str
    email: str
    verified: bool = False
    verified_at: datetime = None

class DataManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        
        self.products_file = os.path.join(data_dir, "products.json")
        self.orders_file = os.path.join(data_dir, "orders.json")
        self.emails_file = os.path.join(data_dir, "user_emails.json")
        
        self.products = self._load_data(self.products_file, {})
        self.orders = self._load_data(self.orders_file, {})
        self.user_emails = self._load_data(self.emails_file, {})
        
        # 内存中的购物车和支付监控
        self.carts: Dict[str, List[Dict]] = {}
        self.payment_monitors: Dict[str, asyncio.Task] = {}

    def _load_data(self, filepath: str, default):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载数据文件失败 {filepath}: {e}")
        return default

    def _save_data(self, filepath: str, data):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"保存数据文件失败 {filepath}: {e}")

    def save_products(self):
        self._save_data(self.products_file, self.products)

    def save_orders(self):
        self._save_data(self.orders_file, self.orders)

    def save_user_emails(self):
        self._save_data(self.emails_file, self.user_emails)

class EmailService:
    def __init__(self, config: Dict):
        self.config = config
        self.enabled = all([
            config.get('smtp_host'),
            config.get('smtp_username'),
            config.get('smtp_password')
        ])

    async def send_email(self, to_email: str, subject: str, content: str) -> bool:
        if not self.enabled:
            logger.warning("邮箱服务未配置")
            return False

        try:
            import aiosmtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            message = MIMEMultipart()
            message['From'] = f"{self.config.get('from_name', '商城系统')} <{self.config['smtp_username']}>"
            message['To'] = to_email
            message['Subject'] = subject

            message.attach(MIMEText(content, 'html', 'utf-8'))

            await aiosmtplib.send(
                message,
                hostname=self.config['smtp_host'],
                port=self.config.get('smtp_port', 587),
                username=self.config['smtp_username'],
                password=self.config['smtp_password'],
                start_tls=True
            )
            logger.info(f"邮件发送成功: {to_email}")
            return True
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            return False

    async def send_verification_code(self, to_email: str, code: str) -> bool:
        subject = "邮箱验证码 - 商城系统"
        content = f"""
        <h3>您的邮箱验证码</h3>
        <p>验证码：<strong>{code}</strong></p>
        <p>该验证码10分钟内有效，请勿泄露给他人。</p>
        """
        return await self.send_email(to_email, subject, content)

    async def send_delivery_notification(self, to_email: str, order: Order, delivery_content: str) -> bool:
        subject = f"订单发货通知 - {order.order_no}"
        content = f"""
        <h3>您的订单已发货</h3>
        <p>订单号：{order.order_no}</p>
        <p>商品：{order.product_name}</p>
        <p>数量：{order.quantity}</p>
        <p>金额：{order.amount}元</p>
        <p>发货内容：</p>
        <pre>{delivery_content}</pre>
        <p>感谢您的购买！</p>
        """
        return await self.send_email(to_email, subject, content)

    async def send_admin_notification(self, admin_email: str, order: Order) -> bool:
        subject = "手动发货通知 - 需要管理员处理"
        content = f"""
        <h3>新的订单需要手动发货</h3>
        <p>订单号：{order.order_no}</p>
        <p>用户ID：{order.user_id}</p>
        <p>用户邮箱：{order.user_email}</p>
        <p>商品：{order.product_name}</p>
        <p>数量：{order.quantity}</p>
        <p>金额：{order.amount}元</p>
        <p>请及时登录系统处理此订单。</p>
        """
        return await self.send_email(admin_email, subject, content)

class PaymentService:
    def __init__(self, config: Dict):
        self.config = config
        self.pid = config.get('pid', '')
        self.key = config.get('key', '')
        self.api_url = config.get('api_url', '/xpay/epay/submit.php')

    def generate_sign(self, params: Dict) -> str:
        """生成支付签名"""
        params_sorted = sorted(params.items())
        sign_str = '&'.join([f"{k}={v}" for k, v in params_sorted if v and k != 'sign'])
        sign_str += f"&key={self.key}"
        return self._md5(sign_str)

    def _md5(self, s: str) -> str:
        return hashlib.md5(s.encode('utf-8')).hexdigest()

    async def create_payment(self, order_no: str, amount: float, product_name: str, 
                           notify_url: str, return_url: str) -> Dict[str, Any]:
        """创建支付订单"""
        params = {
            'pid': self.pid,
            'type': 'alipay',
            'out_trade_no': order_no,
            'notify_url': notify_url,
            'return_url': return_url,
            'name': product_name,
            'money': f"{amount:.2f}",
            'sitename': 'AstrBot商城',
            'device': 'pc'
        }
        
        params['sign'] = self.generate_sign(params)
        params['sign_type'] = 'MD5'

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, data=params) as response:
                    if response.status == 200:
                        result = await response.text()
                        # 这里需要根据沐云支付的实际返回格式进行解析
                        # 假设返回的是JSON格式：{"code":1, "msg":"成功", "data":{"payment_url":"..."}}
                        try:
                            result_json = await response.json()
                            if result_json.get('code') == 1:
                                return {
                                    'success': True, 
                                    'payment_url': result_json['data']['payment_url']
                                }
                            else:
                                return {
                                    'success': False, 
                                    'error': result_json.get('msg', '支付创建失败')
                                }
                        except:
                            # 如果不是JSON格式，直接返回文本
                            return {
                                'success': True, 
                                'payment_url': result
                            }
                    else:
                        return {
                            'success': False, 
                            'error': f'HTTP {response.status}'
                        }
        except Exception as e:
            return {
                'success': False, 
                'error': str(e)
            }

    def generate_qr_code(self, payment_url: str) -> BytesIO:
        """生成支付二维码"""
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(payment_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        img_buffer = BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        return img_buffer

@register("mall", "商城系统", "完整的商城系统插件", "1.0.0")
class MallPlugin(Star):
    def __init__(self, context: Context, config: Dict):
        super().__init__(context)
        self.config = config
        self.data_dir = os.path.join("data", "mall_plugin")
        
        # 初始化服务
        self.data_manager = DataManager(self.data_dir)
        self.email_service = EmailService(config.get('email_config', {}))
        self.payment_service = PaymentService(config.get('muyun_pay', {}))
        
        # 支付超时时间
        self.payment_timeout = config.get('payment_timeout', 60)
        
        # 启动定时任务清理过期订单
        asyncio.create_task(self._cleanup_expired_orders())

    async def _cleanup_expired_orders(self):
        """定时清理过期订单"""
        while True:
            await asyncio.sleep(300)  # 每5分钟检查一次
            try:
                current_time = datetime.now()
                expired_orders = []
                
                for order_no, order_data in self.data_manager.orders.items():
                    if (order_data.get('status') == 'pending' and 
                        order_data.get('expire_time') and
                        datetime.fromisoformat(order_data['expire_time']) < current_time):
                        expired_orders.append(order_no)
                
                for order_no in expired_orders:
                    self.data_manager.orders[order_no]['status'] = 'expired'
                    logger.info(f"订单已过期: {order_no}")
                
                if expired_orders:
                    self.data_manager.save_orders()
                    
            except Exception as e:
                logger.error(f"清理过期订单失败: {e}")

    # 用户邮箱绑定功能
    @filter.command("bind_email")
    async def bind_email(self, event: AstrMessageEvent, email: str):
        """绑定邮箱"""
        user_id = event.get_sender_id()
        
        # 生成验证码
        verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
        
        # 保存验证码（临时存储）
        verification_key = f"verify_{user_id}"
        # 这里应该使用更安全的存储方式，暂时用内存存储
        setattr(self, verification_key, {
            'code': verification_code,
            'email': email,
            'expire_time': datetime.now() + timedelta(minutes=10)
        })
        
        # 发送验证邮件
        success = await self.email_service.send_verification_code(email, verification_code)
        
        if success:
            yield event.plain_result(f"验证码已发送到 {email}，请使用 /verify_email 验证码 完成绑定")
        else:
            yield event.plain_result("邮件发送失败，请检查邮箱地址或联系管理员")

    @filter.command("verify_email")
    async def verify_email(self, event: AstrMessageEvent, code: str):
        """验证邮箱"""
        user_id = event.get_sender_id()
        verification_key = f"verify_{user_id}"
        
        verification_data = getattr(self, verification_key, None)
        if not verification_data or verification_data['expire_time'] < datetime.now():
            yield event.plain_result("验证码已过期，请重新绑定邮箱")
            return
        
        if verification_data['code'] == code:
            # 保存邮箱绑定
            user_email = UserEmail(
                user_id=user_id,
                email=verification_data['email'],
                verified=True,
                verified_at=datetime.now()
            )
            
            self.data_manager.user_emails[user_id] = asdict(user_email)
            self.data_manager.save_user_emails()
            
            # 清理验证数据
            delattr(self, verification_key)
            
            yield event.plain_result("邮箱绑定成功！")
        else:
            yield event.plain_result("验证码错误，请重新输入")

    # 商品管理功能（管理员）
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("add_product")
    async def add_product(self, event: AstrMessageEvent, name: str, price: float, 
                         quantity: int, delivery_type: str = "manual", description: str = ""):
        """添加商品"""
        product_id = str(len(self.data_manager.products) + 1)
        
        product = Product(
            id=product_id,
            name=name,
            price=price,
            quantity=quantity,
            delivery_type=delivery_type,
            description=description
        )
        
        self.data_manager.products[product_id] = asdict(product)
        self.data_manager.save_products()
        
        yield event.plain_result(f"商品添加成功！ID: {product_id}")

    @filter.command("products")
    async def list_products(self, event: AstrMessageEvent):
        """查看商品列表"""
        if not self.data_manager.products:
            yield event.plain_result("暂无商品")
            return
        
        product_list = "🛍️ 商品列表：\n\n"
        for product_id, product in self.data_manager.products.items():
            if product.get('status') == 'active':
                product_list += f"🔸 {product_id}. {product['name']}\n"
                product_list += f"   价格：¥{product['price']} | 库存：{product['quantity']}件\n"
                product_list += f"   发货：{'自动发货' if product['delivery_type'] == 'auto' else '手动发货'}\n"
                if product['description']:
                    product_list += f"   描述：{product['description']}\n"
                product_list += "\n"
        
        product_list += "使用 /buy 商品ID 数量 购买商品"
        yield event.plain_result(product_list)

    @filter.command("buy")
    async def buy_product(self, event: AstrMessageEvent, product_id: str, quantity: int = 1):
        """购买商品"""
        user_id = event.get_sender_id()
        
        # 检查邮箱绑定
        if user_id not in self.data_manager.user_emails:
            yield event.plain_result("请先绑定邮箱！使用 /bind_email 邮箱地址")
            return
        
        user_email = self.data_manager.user_emails[user_id]
        if not user_email.get('verified', False):
            yield event.plain_result("邮箱未验证，请先完成邮箱验证")
            return
        
        # 检查商品
        if product_id not in self.data_manager.products:
            yield event.plain_result("商品不存在")
            return
        
        product = self.data_manager.products[product_id]
        if product.get('status') != 'active':
            yield event.plain_result("商品已下架")
            return
        
        if product['quantity'] < quantity:
            yield event.plain_result(f"库存不足，当前库存：{product['quantity']}件")
            return
        
        if quantity <= 0:
            yield event.plain_result("购买数量必须大于0")
            return
        
        # 创建订单
        order_no = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
        amount = product['price'] * quantity
        expire_time = datetime.now() + timedelta(seconds=self.payment_timeout)
        
        order = Order(
            order_no=order_no,
            user_id=user_id,
            product_id=product_id,
            product_name=product['name'],
            quantity=quantity,
            amount=amount,
            status='pending',
            delivery_type=product['delivery_type'],
            user_email=user_email['email'],
            expire_time=expire_time,
            created_at=datetime.now()
        )
        
        # 生成支付信息
        payment_result = await self.payment_service.create_payment(
            order_no=order_no,
            amount=amount,
            product_name=product['name'],
            notify_url=f"http://your-domain.com/payment/notify",  # 需要配置实际的回调地址
            return_url=f"http://your-domain.com/payment/return"
        )
        
        if not payment_result['success']:
            yield event.plain_result(f"支付创建失败: {payment_result.get('error', '未知错误')}")
            return
        
        # 生成支付二维码
        qr_buffer = self.payment_service.generate_qr_code(payment_result['payment_url'])
        
        # 保存订单
        order.payment_url = payment_result['payment_url']
        self.data_manager.orders[order_no] = asdict(order)
        self.data_manager.save_orders()
        
        # 启动支付监控
        self._start_payment_monitor(order_no)
        
        # 发送支付信息
        yield event.plain_result(
            f"💰 订单创建成功！\n"
            f"📦 商品：{product['name']}\n"
            f"📊 数量：{quantity}件\n"
            f"💰 金额：¥{amount}\n"
            f"⏰ 请在{self.payment_timeout}秒内完成支付\n"
            f"📋 订单号：{order_no}"
        )
        
        # 发送支付二维码
        yield event.image_result(qr_buffer)
        
        # 发送支付链接
        yield event.plain_result(f"支付链接：{payment_result['payment_url']}")

    def _start_payment_monitor(self, order_no: str):
        """启动支付监控"""
        async def monitor_payment():
            await asyncio.sleep(self.payment_timeout)
            
            if order_no in self.data_manager.orders:
                order_data = self.data_manager.orders[order_no]
                if order_data.get('status') == 'pending':
                    # 订单超时，自动取消
                    order_data['status'] = 'expired'
                    self.data_manager.save_orders()
                    logger.info(f"订单超时取消: {order_no}")

        self.data_manager.payment_monitors[order_no] = asyncio.create_task(monitor_payment())

    # 支付回调处理（需要配置webhook）
    @filter.command("check_order")
    async def check_order(self, event: AstrMessageEvent, order_no: str = ""):
        """查看订单状态"""
        user_id = event.get_sender_id()
        
        if order_no:
            # 查看特定订单
            if order_no not in self.data_manager.orders:
                yield event.plain_result("订单不存在")
                return
            
            order_data = self.data_manager.orders[order_no]
            if order_data['user_id'] != user_id and not event.is_admin:
                yield event.plain_result("无权查看此订单")
                return
            
            status_map = {
                'pending': '待支付',
                'paid': '已支付',
                'delivered': '已发货',
                'cancelled': '已取消',
                'expired': '已过期'
            }
            
            status_text = status_map.get(order_data['status'], '未知状态')
            result = f"📋 订单详情：\n订单号：{order_no}\n状态：{status_text}\n商品：{order_data['product_name']}\n数量：{order_data['quantity']}\n金额：¥{order_data['amount']}"
            
            yield event.plain_result(result)
        else:
            # 查看用户所有订单
            user_orders = []
            for o_no, o_data in self.data_manager.orders.items():
                if o_data['user_id'] == user_id:
                    user_orders.append((o_no, o_data))
            
            if not user_orders:
                yield event.plain_result("您还没有订单")
                return
            
            user_orders.sort(key=lambda x: x[1].get('created_at', ''), reverse=True)
            
            order_list = "📋 您的订单：\n\n"
            for o_no, o_data in user_orders[:10]:  # 显示最近10个订单
                status_map = {
                    'pending': '待支付',
                    'paid': '已支付',
                    'delivered': '已发货',
                    'cancelled': '已取消',
                    'expired': '已过期'
                }
                status_text = status_map.get(o_data['status'], '未知')
                order_list += f"🔸 {o_no} - {o_data['product_name']} - {status_text}\n"
            
            order_list += "\n使用 /check_order 订单号 查看详情"
            yield event.plain_result(order_list)

    # 管理员发货功能
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("deliver_order")
    async def deliver_order(self, event: AstrMessageEvent, order_no: str, delivery_content: str = ""):
        """手动发货"""
        if order_no not in self.data_manager.orders:
            yield event.plain_result("订单不存在")
            return
        
        order_data = self.data_manager.orders[order_no]
        if order_data['status'] != 'paid':
            yield event.plain_result("订单未支付或已处理")
            return
        
        # 更新订单状态
        order_data['status'] = 'delivered'
        order_data['delivered_at'] = datetime.now().isoformat()
        self.data_manager.save_orders()
        
        # 发送邮件通知用户
        if delivery_content:
            order_obj = Order(**order_data)
            email_success = await self.email_service.send_delivery_notification(
                order_data['user_email'], order_obj, delivery_content
            )
            
            if email_success:
                yield event.plain_result(f"订单 {order_no} 发货成功，已邮件通知用户")
            else:
                yield event.plain_result(f"订单 {order_no} 发货成功，但邮件发送失败")
        else:
            yield event.plain_result(f"订单 {order_no} 发货成功")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mall_stats")
    async def mall_stats(self, event: AstrMessageEvent):
        """商城统计"""
        total_products = len(self.data_manager.products)
        total_orders = len(self.data_manager.orders)
        total_users = len(self.data_manager.user_emails)
        
        revenue = sum(order['amount'] for order in self.data_manager.orders.values() 
                    if order['status'] in ['paid', 'delivered'])
        
        stats = f"📊 商城统计\n\n"
        stats += f"📦 商品数量：{total_products}\n"
        stats += f"📋 订单总数：{total_orders}\n"
        stats += f"💰 总营业额：¥{revenue:.2f}\n"
        stats += f"👥 注册用户：{total_users}"
        
        yield event.plain_result(stats)

    # 支付成功回调处理
    async def handle_payment_notify(self, order_no: str):
        """处理支付成功回调"""
        if order_no not in self.data_manager.orders:
            return False
        
        order_data = self.data_manager.orders[order_no]
        if order_data['status'] != 'pending':
            return False
        
        # 更新订单状态
        order_data['status'] = 'paid'
        order_data['paid_at'] = datetime.now().isoformat()
        self.data_manager.save_orders()
        
        # 根据发货类型处理
        if order_data['delivery_type'] == 'auto':
            # 自动发货
            await self._auto_deliver(order_no)
        else:
            # 手动发货 - 通知管理员
            await self._notify_admin_for_manual_delivery(order_no)
        
        return True

    async def _auto_deliver(self, order_no: str):
        """自动发货"""
        order_data = self.data_manager.orders[order_no]
        # 生成一个随机的卡密并发送给用户
        card_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        delivery_content = f"您的商品卡密：{card_code}\n请妥善保管，勿泄露给他人"
        
        # 发送邮件通知
        order_obj = Order(**order_data)
        email_success = await self.email_service.send_delivery_notification(
            order_data['user_email'], order_obj, delivery_content
        )
        
        # 同时通过机器人发送消息给用户
        user_umo = order_data.get('user_unified_msg_origin')
        if user_umo:
            message_chain = [
                Comp.Plain(text=f"✅ 您的订单 {order_no} 已自动发货\n"),
                Comp.Plain(text=f"📦 商品：{order_data['product_name']}\n"),
                Comp.Plain(text=f"🔑 发货内容：{delivery_content}")
            ]
            await self.context.send_message(user_umo, message_chain)
        
        # 记录发货日志
        delivery_log = {
            'order_id': order_no,
            'delivery_type': 'auto',
            'content': delivery_content,
            'delivered_by': 'system',
            'created_at': datetime.now().isoformat()
        }
        
        # 更新库存
        product_id = order_data['product_id']
        if product_id in self.data_manager.products:
            self.data_manager.products[product_id]['quantity'] -= order_data['quantity']
            self.data_manager.save_products()
        
        logger.info(f"订单 {order_no} 自动发货完成")

    async def _notify_admin_for_manual_delivery(self, order_no: str):
        """通知管理员手动发货"""
        order_data = self.data_manager.orders[order_no]
        
        # 获取管理员邮箱（从配置中读取或使用默认）
        admin_email = self.config.get('admin_email', 'admin@example.com')
        
        # 发送邮件通知管理员
        order_obj = Order(**order_data)
        email_success = await self.email_service.send_admin_notification(admin_email, order_obj)
        
        if email_success:
            logger.info(f"已发送手动发货通知给管理员，订单：{order_no}")
        else:
            logger.error(f"发送管理员通知失败，订单：{order_no}")
        
        # 同时通过机器人通知在线管理员
        admin_message = (
            f"🛎️ 新的手动发货订单\n"
            f"📋 订单号：{order_no}\n"
            f"👤 用户：{order_data['user_id']}\n"
            f"📧 邮箱：{order_data['user_email']}\n"
            f"📦 商品：{order_data['product_name']} × {order_data['quantity']}\n"
            f"💰 金额：¥{order_data['amount']}\n"
            f"⏰ 时间：{order_data.get('paid_at', '未知')}\n"
            f"请使用 /deliver_order {order_no} 发货内容 进行处理"
        )
        
        # 这里需要获取管理员的会话标识，实际应用中可能需要从配置或数据库读取
        # 暂时注释，需要根据实际情况实现
        # admin_umo = "获取管理员的unified_msg_origin"
        # if admin_umo:
        #     await self.context.send_message(admin_umo, [Comp.Plain(text=admin_message)])

    # 购物车功能
    @filter.command("cart_add")
    async def add_to_cart(self, event: AstrMessageEvent, product_id: str, quantity: int = 1):
        """添加商品到购物车"""
        user_id = event.get_sender_id()
        
        # 检查邮箱绑定
        if user_id not in self.data_manager.user_emails:
            yield event.plain_result("请先绑定邮箱！使用 /bind_email 邮箱地址")
            return
        
        # 检查商品
        if product_id not in self.data_manager.products:
            yield event.plain_result("商品不存在")
            return
        
        product = self.data_manager.products[product_id]
        if product.get('status') != 'active':
            yield event.plain_result("商品已下架")
            return
        
        if product['quantity'] < quantity:
            yield event.plain_result(f"库存不足，当前库存：{product['quantity']}件")
            return
        
        # 初始化用户购物车
        if user_id not in self.data_manager.carts:
            self.data_manager.carts[user_id] = []
        
        # 检查是否已存在相同商品
        cart_updated = False
        for item in self.data_manager.carts[user_id]:
            if item['product_id'] == product_id:
                item['quantity'] += quantity
                cart_updated = True
                break
        
        if not cart_updated:
            self.data_manager.carts[user_id].append({
                "product_id": product_id,
                "name": product['name'],
                "price": product['price'],
                "quantity": quantity,
                "delivery_type": product['delivery_type']
            })
        
        yield event.plain_result(f"✅ 已成功将 {quantity} 件 {product['name']} 加入购物车")

    @filter.command("cart")
    async def view_cart(self, event: AstrMessageEvent):
        """查看购物车"""
        user_id = event.get_sender_id()
        
        if user_id not in self.data_manager.carts or not self.data_manager.carts[user_id]:
            yield event.plain_result("🛒 您的购物车是空的")
            return
        
        cart_content = "🛒 购物车内容：\n\n"
        total_price = 0
        
        for i, item in enumerate(self.data_manager.carts[user_id], 1):
            item_total = item['price'] * item['quantity']
            total_price += item_total
            cart_content += f"{i}. {item['name']}\n"
            cart_content += f"   单价：¥{item['price']} × {item['quantity']}件 = ¥{item_total}\n"
            cart_content += f"   发货：{'自动' if item['delivery_type'] == 'auto' else '手动'}\n\n"
        
        cart_content += f"💰 总计：¥{total_price}\n\n"
        cart_content += "使用 /cart_buy 购买购物车所有商品\n"
        cart_content += "使用 /cart_remove <序号> 移除商品\n"
        cart_content += "使用 /cart_clear 清空购物车"
        
        yield event.plain_result(cart_content)

    @filter.command("cart_remove")
    async def remove_from_cart(self, event: AstrMessageEvent, index: int):
        """从购物车移除商品"""
        user_id = event.get_sender_id()
        
        if user_id not in self.data_manager.carts or not self.data_manager.carts[user_id]:
            yield event.plain_result("❌ 购物车为空")
            return
        
        if index < 1 or index > len(self.data_manager.carts[user_id]):
            yield event.plain_result("❌ 商品序号无效")
            return
        
        removed_item = self.data_manager.carts[user_id].pop(index - 1)
        
        # 如果购物车为空，删除整个购物车
        if not self.data_manager.carts[user_id]:
            del self.data_manager.carts[user_id]
        
        yield event.plain_result(f"✅ 已从购物车移除 {removed_item['name']}")

    @filter.command("cart_clear")
    async def clear_cart(self, event: AstrMessageEvent):
        """清空购物车"""
        user_id = event.get_sender_id()
        
        if user_id in self.data_manager.carts:
            del self.data_manager.carts[user_id]
            yield event.plain_result("✅ 购物车已清空")
        else:
            yield event.plain_result("🛒 购物车已经是空的")

    @filter.command("cart_buy")
    async def buy_cart(self, event: AstrMessageEvent):
        """购买购物车所有商品"""
        user_id = event.get_sender_id()
        
        # 检查邮箱绑定
        if user_id not in self.data_manager.user_emails:
            yield event.plain_result("请先绑定邮箱！使用 /bind_email 邮箱地址")
            return
        
        user_email = self.data_manager.user_emails[user_id]
        if not user_email.get('verified', False):
            yield event.plain_result("邮箱未验证，请先完成邮箱验证")
            return
        
        if user_id not in self.data_manager.carts or not self.data_manager.carts[user_id]:
            yield event.plain_result("❌ 购物车为空")
            return
        
        # 检查库存
        for item in self.data_manager.carts[user_id]:
            product = self.data_manager.products.get(item['product_id'])
            if not product or product.get('status') != 'active':
                yield event.plain_result(f"❌ 商品 {item['name']} 已下架")
                return
            
            if product['quantity'] < item['quantity']:
                yield event.plain_result(f"❌ {item['name']} 库存不足，当前库存：{product['quantity']}件")
                return
        
        # 创建合并订单
        order_no = f"CART{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
        total_amount = sum(item['price'] * item['quantity'] for item in self.data_manager.carts[user_id])
        expire_time = datetime.now() + timedelta(seconds=self.payment_timeout)
        
        # 创建订单
        order = Order(
            order_no=order_no,
            user_id=user_id,
            product_id="cart",  # 特殊标识，表示是购物车订单
            product_name="购物车商品",
            quantity=sum(item['quantity'] for item in self.data_manager.carts[user_id]),
            amount=total_amount,
            status='pending',
            delivery_type='mixed',  # 混合发货
            user_email=user_email['email'],
            expire_time=expire_time,
            created_at=datetime.now()
        )
        
        # 保存购物车商品详情
        order.cart_items = [
            {
                'product_id': item['product_id'],
                'name': item['name'],
                'price': item['price'],
                'quantity': item['quantity'],
                'delivery_type': item['delivery_type']
            }
            for item in self.data_manager.carts[user_id]
        ]
        
        # 生成支付信息
        payment_result = await self.payment_service.create_payment(
            order_no=order_no,
            amount=total_amount,
            product_name="购物车商品",
            notify_url=f"http://your-domain.com/payment/notify",
            return_url=f"http://your-domain.com/payment/return"
        )
        
        if not payment_result['success']:
            yield event.plain_result(f"支付创建失败: {payment_result.get('error', '未知错误')}")
            return
        
        # 生成支付二维码
        qr_buffer = self.payment_service.generate_qr_code(payment_result['payment_url'])
        
        # 保存订单
        order.payment_url = payment_result['payment_url']
        self.data_manager.orders[order_no] = asdict(order)
        self.data_manager.save_orders()
        
        # 启动支付监控
        self._start_payment_monitor(order_no)
        
        # 清空购物车
        del self.data_manager.carts[user_id]
        
        # 发送支付信息
        yield event.plain_result(
            f"🛒 购物车订单创建成功！\n"
            f"📦 商品数量：{len(order.cart_items)} 种\n"
            f"📊 总数量：{order.quantity} 件\n"
            f"💰 总金额：¥{total_amount}\n"
            f"⏰ 请在{self.payment_timeout}秒内完成支付\n"
            f"📋 订单号：{order_no}"
        )
        
        # 发送支付二维码
        yield event.image_result(qr_buffer)
        
        # 发送支付链接
        yield event.plain_result(f"支付链接：{payment_result['payment_url']}")

    # 订单管理功能
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("order_list")
    async def list_orders(self, event: AstrMessageEvent, status: str = "all", page: int = 1):
        """管理员查看订单列表"""
        page_size = 10
        filtered_orders = []
        
        for order_no, order_data in self.data_manager.orders.items():
            if status == "all" or order_data.get('status') == status:
                filtered_orders.append((order_no, order_data))
        
        # 按创建时间倒序排列
        filtered_orders.sort(key=lambda x: x[1].get('created_at', ''), reverse=True)
        
        total_orders = len(filtered_orders)
        total_pages = (total_orders + page_size - 1) // page_size
        start_idx = (page - 1) * page_size
        end_idx = min(start_idx + page_size, total_orders)
        
        if not filtered_orders:
            yield event.plain_result("暂无订单")
            return
        
        order_list = f"📋 订单列表 (第{page}/{total_pages}页)\n\n"
        
        status_map = {
            'pending': '⏳待支付',
            'paid': '✅已支付',
            'delivered': '🚚已发货',
            'cancelled': '❌已取消',
            'expired': '💸已过期'
        }
        
        for i in range(start_idx, end_idx):
            order_no, order_data = filtered_orders[i]
            status_text = status_map.get(order_data.get('status', 'unknown'), '❓未知')
            
            order_list += f"{i+1}. {order_no}\n"
            order_list += f"   状态：{status_text}\n"
            order_list += f"   商品：{order_data.get('product_name', 'N/A')}\n"
            order_list += f"   金额：¥{order_data.get('amount', 0)}\n"
            order_list += f"   用户：{order_data.get('user_id', '')}\n"
            order_list += f"   时间：{order_data.get('created_at', '')[:19]}\n\n"
        
        order_list += f"共 {total_orders} 个订单\n"
        if page < total_pages:
            order_list += f"使用 /order_list {status} {page+1} 查看下一页"
        
        yield event.plain_result(order_list)

    @filter.command("cancel_order")
    async def cancel_order(self, event: AstrMessageEvent, order_no: str):
        """取消订单"""
        user_id = event.get_sender_id()
        
        if order_no not in self.data_manager.orders:
            yield event.plain_result("订单不存在")
            return
        
        order_data = self.data_manager.orders[order_no]
        
        # 检查权限：用户只能取消自己的订单，管理员可以取消任何订单
        if order_data['user_id'] != user_id and not event.is_admin:
            yield event.plain_result("无权操作此订单")
            return
        
        if order_data['status'] not in ['pending']:
            yield event.plain_result("只有待支付的订单可以取消")
            return
        
        # 取消订单
        order_data['status'] = 'cancelled'
        order_data['cancelled_at'] = datetime.now().isoformat()
        order_data['cancelled_by'] = 'user' if order_data['user_id'] == user_id else 'admin'
        
        self.data_manager.save_orders()
        
        # 如果订单有支付监控任务，取消它
        if order_no in self.data_manager.payment_monitors:
            self.data_manager.payment_monitors[order_no].cancel()
            del self.data_manager.payment_monitors[order_no]
        
        yield event.plain_result(f"✅ 订单 {order_no} 已取消")

    # 数据备份和恢复功能
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("backup_data")
    async def backup_data(self, event: AstrMessageEvent):
        """备份数据"""
        import shutil
        import tempfile
        import zipfile
        
        try:
            # 创建临时备份目录
            backup_dir = tempfile.mkdtemp()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"mall_backup_{timestamp}.zip")
            
            # 创建ZIP文件
            with zipfile.ZipFile(backup_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # 备份所有数据文件
                for filename in [self.data_manager.products_file, 
                               self.data_manager.orders_file, 
                               self.data_manager.emails_file]:
                    if os.path.exists(filename):
                        zipf.write(filename, os.path.basename(filename))
            
            # 读取备份文件内容
            with open(backup_file, 'rb') as f:
                backup_data = f.read()
            
            # 清理临时文件
            shutil.rmtree(backup_dir)
            
            # 发送备份文件
            yield event.file_result(backup_data, f"mall_backup_{timestamp}.zip")
            yield event.plain_result("✅ 数据备份完成")
            
        except Exception as e:
            logger.error(f"数据备份失败: {e}")
            yield event.plain_result("❌ 数据备份失败")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("restore_data")
    async def restore_data(self, event: AstrMessageEvent):
        """恢复数据（需要上传备份文件）"""
        # 这个功能需要处理文件上传，在AstrBot中可能需要特殊处理
        # 这里先提供基本框架
        yield event.plain_result("数据恢复功能需要文件上传支持，请参考AstrBot文档实现文件上传处理")

    # 系统状态检查
    @filter.command("mall_status")
    async def mall_status(self, event: AstrMessageEvent):
        """检查系统状态"""
        status_report = "🏪 商城系统状态\n\n"
        
        # 基本统计
        total_products = len(self.data_manager.products)
        total_orders = len(self.data_manager.orders)
        total_users = len(self.data_manager.user_emails)
        active_carts = len(self.data_manager.carts)
        
        # 订单状态统计
        status_count = {'pending': 0, 'paid': 0, 'delivered': 0, 'cancelled': 0, 'expired': 0}
        for order_data in self.data_manager.orders.values():
            status = order_data.get('status', 'unknown')
            if status in status_count:
                status_count[status] += 1
        
        revenue = sum(order_data['amount'] for order_data in self.data_manager.orders.values() 
                     if order_data.get('status') in ['paid', 'delivered'])
        
        status_report += f"📦 商品数量：{total_products}\n"
        status_report += f"📋 订单总数：{total_orders}\n"
        status_report += f"👥 注册用户：{total_users}\n"
        status_report += f"🛒 活跃购物车：{active_carts}\n"
        status_report += f"💰 总营业额：¥{revenue:.2f}\n\n"
        
        status_report += "📊 订单状态分布：\n"
        status_report += f"⏳ 待支付：{status_count['pending']}\n"
        status_report += f"✅ 已支付：{status_count['paid']}\n"
        status_report += f"🚚 已发货：{status_count['delivered']}\n"
        status_report += f"❌ 已取消：{status_count['cancelled']}\n"
        status_report += f"💸 已过期：{status_count['expired']}\n\n"
        
        # 服务状态
        email_status = "✅ 正常" if self.email_service.enabled else "❌ 未配置"
        payment_status = "✅ 正常" if self.payment_service.pid else "❌ 未配置"
        
        status_report += f"📧 邮件服务：{email_status}\n"
        status_report += f"💳 支付服务：{payment_status}\n"
        status_report += f"⏰ 支付超时：{self.payment_timeout}秒"
        
        yield event.plain_result(status_report)

    async def terminate(self):
        """插件卸载时保存数据"""
        try:
            # 取消所有支付监控任务
            for task in self.data_manager.payment_monitors.values():
                task.cancel()
            
            # 保存所有数据
            self.data_manager.save_products()
            self.data_manager.save_orders()
            self.data_manager.save_user_emails()
            
            logger.info("商城插件数据已保存")
        except Exception as e:
            logger.error(f"插件终止时发生错误: {e}")

    # 支付回调处理（Webhook端点）
    # 注意：这需要在AstrBot外部实现，或者通过特殊指令模拟
    @filter.command("payment_callback")
    async def payment_callback(self, event: AstrMessageEvent, order_no: str, status: str):
        """模拟支付回调（用于测试）"""
        if not event.is_admin:
            yield event.plain_result("无权操作")
            return
        
        if status == "success":
            success = await self.handle_payment_notify(order_no)
            if success:
                yield event.plain_result(f"✅ 订单 {order_no} 支付成功处理完成")
            else:
                yield event.plain_result(f"❌ 订单 {order_no} 处理失败")
        else:
            yield event.plain_result("❌ 支付状态无效")

    # 帮助信息
    @filter.command("mall_help")
    async def mall_help(self, event: AstrMessageEvent):
        """商城帮助信息"""
        help_text = """
🛍️ 商城系统使用指南

👤 用户命令：
/bind_email <邮箱> - 绑定邮箱
/verify_email <验证码> - 验证邮箱
/products - 查看商品列表
/buy <商品ID> [数量] - 购买商品
/cart_add <商品ID> [数量] - 加入购物车
/cart - 查看购物车
/cart_buy - 购买购物车所有商品
/cart_remove <序号> - 移除购物车商品
/cart_clear - 清空购物车
/check_order [订单号] - 查看订单
/cancel_order <订单号> - 取消订单
/mall_status - 查看系统状态

👑 管理员命令：
/add_product <名称> <价格> <库存> [发货方式] [描述] - 添加商品
/order_list [状态] [页码] - 查看订单列表
/deliver_order <订单号> [发货内容] - 手动发货
/mall_stats - 商城统计
/backup_data - 备份数据
/restore_data - 恢复数据
/payment_callback <订单号> <状态> - 模拟支付回调

💡 提示：
1. 首次使用请先绑定邮箱
2. 支付超时时间为60秒
3. 自动发货商品支付后立即发货
4. 手动发货商品需要管理员处理
        """
        
        yield event.plain_result(help_text)

    # 会话控制示例：商品咨询
    @filter.command("consult")
    async def start_consultation(self, event: AstrMessageEvent, product_id: str = ""):
        """开始商品咨询"""
        if product_id and product_id in self.data_manager.products:
            product = self.data_manager.products[product_id]
            yield event.plain_result(f"💬 开始咨询商品：{product['name']}\n请描述您的问题，输入'结束'退出咨询")
        else:
            yield event.plain_result("💬 开始客服咨询，请输入您的问题，输入'结束'退出咨询")
        
        @session_waiter(timeout=300, record_history_chains=False)  # 5分钟超时
        async def consultation_waiter(controller: SessionController, consult_event: AstrMessageEvent):
            user_message = consult_event.message_str
            
            if user_message.strip() in ['结束', '退出', 'end', 'quit']:
                await consult_event.send(consult_event.plain_result("感谢您的咨询，再见！"))
                controller.stop()
                return
            
            # 这里可以接入客服系统或AI回复
            # 简单示例：模拟客服回复
            responses = [
                "好的，我了解您的问题，请稍等为您查询...",
                "这个问题我们需要进一步核实，请您耐心等待",
                "感谢您的反馈，我们会尽快处理",
                "请问您能提供更多详细信息吗？"
            ]
            import random
            response = random.choice(responses)
            
            await consult_event.send(consult_event.plain_result(response))
            controller.keep(timeout=300, reset_timeout=True)
        
        try:
            await consultation_waiter(event)
        except TimeoutError:
            yield event.plain_result("咨询会话已超时结束")
        except Exception as e:
            logger.error(f"咨询会话异常: {e}")
            yield event.plain_result("咨询过程发生错误")
        finally:
            event.stop_event()
