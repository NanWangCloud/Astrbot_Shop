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
from collections import defaultdict

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
    auto_delivery_content: str = ""  # 自动发货内容
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
    payment_method: str = ""  # 支付方式
    qr_code_path: str = ""
    expire_time: datetime = None
    created_at: datetime = None
    paid_at: datetime = None
    cart_items: Optional[List[Dict]] = None  # 购物车商品详情

@dataclass
class UserEmail:
    user_id: str
    email: str
    verified: bool = False
    verified_at: datetime = None

@dataclass
class PaymentMethod:
    id: str
    name: str
    type: str  # alipay, wxpay, etc.
    enabled: bool = True
    config: Dict = None

class DataManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        
        self.products_file = os.path.join(data_dir, "products.json")
        self.orders_file = os.path.join(data_dir, "orders.json")
        self.emails_file = os.path.join(data_dir, "user_emails.json")
        self.payment_methods_file = os.path.join(data_dir, "payment_methods.json")
        
        self.products = self._load_data(self.products_file, {})
        self.orders = self._load_data(self.orders_file, {})
        self.user_emails = self._load_data(self.emails_file, {})
        self.payment_methods = self._load_data(self.payment_methods_file, {})
        
        # 初始化默认支付方式
        if not self.payment_methods:
            self._init_default_payment_methods()
        
        # 内存中的购物车和支付监控
        self.carts: Dict[str, List[Dict]] = {}
        self.payment_monitors: Dict[str, asyncio.Task] = {}

    def _init_default_payment_methods(self):
        """初始化默认支付方式"""
        default_methods = {
            "alipay": asdict(PaymentMethod(
                id="alipay",
                name="支付宝",
                type="alipay",
                enabled=True,
                config={}
            )),
            "wxpay": asdict(PaymentMethod(
                id="wxpay",
                name="微信支付",
                type="wxpay",
                enabled=True,
                config={}
            ))
        }
        self.payment_methods = default_methods
        self._save_data(self.payment_methods_file, self.payment_methods)

    def _load_data(self, filepath: str, default):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 转换日期字符串为datetime对象
                    return self._convert_date_strings(data)
        except Exception as e:
            logger.error(f"加载数据文件失败 {filepath}: {e}")
        return default

    def _convert_date_strings(self, data):
        """递归转换日期字符串为datetime对象"""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    # 尝试解析ISO格式的日期字符串
                    try:
                        if len(value) >= 19 and 'T' in value:
                            data[key] = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass
                elif isinstance(value, (dict, list)):
                    data[key] = self._convert_date_strings(value)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    data[i] = self._convert_date_strings(item)
        return data

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
        
    def save_payment_methods(self):
        self._save_data(self.payment_methods_file, self.payment_methods)

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
        self.base_url = config.get('base_url', 'http://your-domain.com')  # 从配置读取

    def generate_sign(self, params: Dict) -> str:
        """生成支付签名"""
        # 过滤空值参数
        params = {k: v for k, v in params.items() if v is not None and v != ''}
        params_sorted = sorted(params.items())
        sign_str = '&'.join([f"{k}={v}" for k, v in params_sorted if k != 'sign'])
        sign_str += f"&key={self.key}"
        return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

    async def create_payment(self, order_no: str, amount: float, product_name: str, 
                           payment_method: str) -> Dict[str, Any]:
        """创建支付订单"""
        # 使用配置中的base_url
        notify_url = f"{self.base_url}/payment/notify"
        return_url = f"{self.base_url}/payment/return"
        
        params = {
            'pid': self.pid,
            'type': payment_method,  # 使用用户选择的支付方式
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
                        # 假设返回的是支付页面HTML或跳转URL
                        return {
                            'success': True, 
                            'payment_url': result,  # 或者从结果中提取支付URL
                            'payment_method': payment_method
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
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(payment_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        img_buffer = BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        return img_buffer

@register("mall", "商城系统", "完整的商城系统插件", "1.1.0")
class MallPlugin(Star):
    def __init__(self, context: Context, config: Dict):
        super().__init__(context)
        self.config = config
        
        # 使用框架提供的工具获取数据目录
        try:
            from astrbot.api.star import StarTools
            self.data_dir = StarTools.get_data_dir()
        except ImportError:
            # 回退方案
            self.data_dir = os.path.join("data", "mall_plugin")
        
        # 初始化服务
        self.data_manager = DataManager(self.data_dir)
        self.email_service = EmailService(config.get('email_config', {}))
        self.payment_service = PaymentService(config.get('muyun_pay', {}))
        
        # 支付超时时间
        self.payment_timeout = config.get('payment_timeout', 60)
        
        # 使用专用字典管理临时状态
        self.temp_orders: Dict[str, Dict] = {}
        
        # 库存锁机制，防止竞态条件
        self.product_locks = defaultdict(asyncio.Lock)
        
        # 插件版本
        self.plugin_version = "1.1.0"
        
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
                        order_data['expire_time'] < current_time):
                        expired_orders.append(order_no)
                
                for order_no in expired_orders:
                    self.data_manager.orders[order_no]['status'] = 'expired'
                    logger.info(f"订单已过期: {order_no}")
                
                if expired_orders:
                    self.data_manager.save_orders()
                    
            except Exception as e:
                logger.error(f"清理过期订单失败: {e}")

    # 支付方式管理
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("add_payment_method")
    async def add_payment_method(self, event: AstrMessageEvent, method_id: str, name: str, 
                               payment_type: str, enabled: bool = True):
        """添加支付方式"""
        if method_id in self.data_manager.payment_methods:
            yield event.plain_result("支付方式ID已存在")
            return
            
        payment_method = PaymentMethod(
            id=method_id,
            name=name,
            type=payment_type,
            enabled=enabled
        )
        
        self.data_manager.payment_methods[method_id] = asdict(payment_method)
        self.data_manager.save_payment_methods()
        
        yield event.plain_result(f"支付方式 {name} 添加成功")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("list_payment_methods")
    async def list_payment_methods(self, event: AstrMessageEvent):
        """查看支付方式列表"""
        if not self.data_manager.payment_methods:
            yield event.plain_result("暂无支付方式")
            return
            
        methods_list = "💳 支付方式列表：\n\n"
        for method_id, method in self.data_manager.payment_methods.items():
            status = "✅ 启用" if method.get('enabled', True) else "❌ 禁用"
            methods_list += f"🔸 {method_id}: {method['name']} ({method['type']}) - {status}\n"
        
        yield event.plain_result(methods_list)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("toggle_payment_method")
    async def toggle_payment_method(self, event: AstrMessageEvent, method_id: str, enabled: bool):
        """启用/禁用支付方式"""
        if method_id not in self.data_manager.payment_methods:
            yield event.plain_result("支付方式不存在")
            return
            
        self.data_manager.payment_methods[method_id]['enabled'] = enabled
        self.data_manager.save_payment_methods()
        
        status = "启用" if enabled else "禁用"
        yield event.plain_result(f"支付方式 {method_id} 已{status}")

    # 商品管理功能（管理员）
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("add_product")
    async def add_product(self, event: AstrMessageEvent, name: str, price: float, 
                         quantity: int, delivery_type: str = "manual", 
                         description: str = "", auto_delivery_content: str = ""):
        """添加商品"""
        product_id = str(len(self.data_manager.products) + 1)
        
        product = Product(
            id=product_id,
            name=name,
            price=price,
            quantity=quantity,
            delivery_type=delivery_type,
            description=description,
            auto_delivery_content=auto_delivery_content
        )
        
        self.data_manager.products[product_id] = asdict(product)
        self.data_manager.save_products()
        
        yield event.plain_result(f"商品添加成功！ID: {product_id}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_auto_delivery")
    async def set_auto_delivery_content(self, event: AstrMessageEvent, product_id: str, content: str):
        """设置自动发货内容"""
        if product_id not in self.data_manager.products:
            yield event.plain_result("商品不存在")
            return
            
        self.data_manager.products[product_id]['auto_delivery_content'] = content
        self.data_manager.save_products()
        
        yield event.plain_result(f"商品 {product_id} 的自动发货内容已设置")

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

    @filter.command("product_info")
    async def product_info(self, event: AstrMessageEvent, product_id: str):
        """查看商品详情"""
        if product_id not in self.data_manager.products:
            yield event.plain_result("商品不存在")
            return
            
        product = self.data_manager.products[product_id]
        info = f"📦 商品详情：{product['name']}\n\n"
        info += f"💰 价格：¥{product['price']}\n"
        info += f"📊 库存：{product['quantity']}件\n"
        info += f"🚚 发货方式：{'自动发货' if product['delivery_type'] == 'auto' else '手动发货'}\n"
        
        if product['description']:
            info += f"📝 描述：{product['description']}\n"
            
        if product['delivery_type'] == 'auto' and product.get('auto_delivery_content'):
            info += f"📨 自动发货内容：{product['auto_delivery_content']}\n"
            
        info += f"\n使用 /buy {product_id} 数量 购买此商品"
        
        yield event.plain_result(info)

    # 购买流程 - 支持选择支付方式
    @filter.command("buy")
    async def buy_product(self, event: AstrMessageEvent, product_id: str, quantity: int = 1):
        """购买商品 - 第一步：显示商品信息和支付方式选择"""
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
        
        if quantity <= 0:
            yield event.plain_result("购买数量必须大于0")
            return
        
        # 使用锁机制检查库存，防止竞态条件
        async with self.product_locks[product_id]:
            if product['quantity'] < quantity:
                yield event.plain_result(f"库存不足，当前库存：{product['quantity']}件")
                return
            
            # 预扣库存（创建订单时预扣，支付成功后再实际扣减）
            # 这里只是检查，不实际扣减
            
        # 显示商品信息和支付方式选择
        amount = product['price'] * quantity
        
        # 获取可用的支付方式
        available_methods = []
        for method_id, method in self.data_manager.payment_methods.items():
            if method.get('enabled', True):
                available_methods.append((method_id, method['name']))
        
        if not available_methods:
            yield event.plain_result("暂无可用支付方式，请联系管理员")
            return
        
        # 保存临时订单信息，用于下一步支付
        temp_order_key = f"temp_order_{user_id}"
        self.temp_orders[user_id] = {
            'product_id': product_id,
            'product_name': product['name'],
            'quantity': quantity,
            'amount': amount,
            'expire_time': datetime.now() + timedelta(minutes=5)  # 5分钟内有效
        }
        
        # 显示商品信息和支付方式选择
        product_info = f"🛒 确认购买信息：\n\n"
        product_info += f"📦 商品：{product['name']}\n"
        product_info += f"📊 数量：{quantity}件\n"
        product_info += f"💰 总价：¥{amount}\n\n"
        product_info += f"💳 请选择支付方式：\n"
        
        for i, (method_id, method_name) in enumerate(available_methods, 1):
            product_info += f"{i}. {method_name}\n"
        
        product_info += f"\n请回复支付方式编号（1-{len(available_methods)}）"
        
        yield event.plain_result(product_info)
        
        # 启动支付方式选择会话
        @session_waiter(timeout=300)  # 5分钟超时
        async def payment_method_waiter(controller: SessionController, wait_event: AstrMessageEvent):
            user_choice = wait_event.message_str.strip()
            
            # 检查临时订单是否过期
            temp_order = self.temp_orders.get(user_id)
            if not temp_order or temp_order['expire_time'] < datetime.now():
                await wait_event.send(wait_event.plain_result("订单已过期，请重新购买"))
                if user_id in self.temp_orders:
                    del self.temp_orders[user_id]
                controller.stop()
                return
            
            # 验证用户选择
            try:
                choice_index = int(user_choice) - 1
                if 0 <= choice_index < len(available_methods):
                    method_id, method_name = available_methods[choice_index]
                    
                    # 创建正式订单
                    await self._create_final_order(
                        wait_event, temp_order, method_id, method_name, user_id, user_email['email']
                    )
                    # 清理临时订单
                    if user_id in self.temp_orders:
                        del self.temp_orders[user_id]
                    controller.stop()
                else:
                    await wait_event.send(wait_event.plain_result(f"无效选择，请输入1-{len(available_methods)}之间的数字"))
                    controller.keep(timeout=300, reset_timeout=True)
            except ValueError:
                await wait_event.send(wait_event.plain_result("请输入数字选择支付方式"))
                controller.keep(timeout=300, reset_timeout=True)
        
        try:
            await payment_method_waiter(event)
        except TimeoutError:
            # 清理临时订单
            if user_id in self.temp_orders:
                del self.temp_orders[user_id]
            yield event.plain_result("支付方式选择超时，请重新购买")
        except Exception as e:
            logger.error(f"支付流程错误: {e}")
            # 清理临时订单
            if user_id in self.temp_orders:
                del self.temp_orders[user_id]
            yield event.plain_result("购买过程发生错误，请稍后重试或联系管理员。")

    async def _create_final_order(self, event, temp_order, method_id, method_name, user_id, user_email):
        """创建最终订单并生成支付"""
        product_id = temp_order['product_id']
        
        # 再次检查库存（双重检查）
        async with self.product_locks[product_id]:
            product = self.data_manager.products[product_id]
            if product['quantity'] < temp_order['quantity']:
                await event.send(event.plain_result(f"库存不足，当前库存：{product['quantity']}件"))
                return
        
        # 创建订单
        order_no = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
        expire_time = datetime.now() + timedelta(seconds=self.payment_timeout)
        
        order = Order(
            order_no=order_no,
            user_id=user_id,
            product_id=product_id,
            product_name=temp_order['product_name'],
            quantity=temp_order['quantity'],
            amount=temp_order['amount'],
            status='pending',
            delivery_type=product['delivery_type'],
            user_email=user_email,
            payment_method=method_name,
            expire_time=expire_time,
            created_at=datetime.now()
        )
        
        # 生成支付信息
        payment_result = await self.payment_service.create_payment(
            order_no=order_no,
            amount=temp_order['amount'],
            product_name=temp_order['product_name'],
            payment_method=method_id
        )
        
        if not payment_result['success']:
            await event.send(event.plain_result(f"支付创建失败: {payment_result.get('error', '未知错误')}"))
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
        await event.send(event.plain_result(
            f"💰 订单创建成功！\n"
            f"📦 商品：{temp_order['product_name']}\n"
            f"📊 数量：{temp_order['quantity']}件\n"
            f"💰 金额：¥{temp_order['amount']}\n"
            f"💳 支付方式：{method_name}\n"
            f"⏰ 请在{self.payment_timeout}秒内完成支付\n"
            f"📋 订单号：{order_no}"
        ))
        
        # 发送支付二维码
        await event.send(event.image_result(qr_buffer))
        
        # 发送支付链接
        await event.send(event.plain_result(f"支付链接：{payment_result['payment_url']}"))

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

    # 支付回调处理
    async def handle_payment_notify(self, order_no: str):
        """处理支付成功回调"""
        if order_no not in self.data_manager.orders:
            return False
        
        order_data = self.data_manager.orders[order_no]
        if order_data['status'] != 'pending':
            return False
        
        # 更新订单状态
        order_data['status'] = 'paid'
        order_data['paid_at'] = datetime.now()
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
        """自动发货 - 使用管理员设置的自动发货内容"""
        order_data = self.data_manager.orders[order_no]
        product_id = order_data['product_id']
        
        # 使用锁机制确保库存扣减的原子性
        async with self.product_locks[product_id]:
            # 再次检查库存
            if product_id in self.data_manager.products:
                product = self.data_manager.products[product_id]
                if product['quantity'] < order_data['quantity']:
                    logger.error(f"库存不足，无法发货订单 {order_no}")
                    return
                
                # 扣减库存
                product['quantity'] -= order_data['quantity']
                self.data_manager.save_products()
        
        # 获取自动发货内容
        if product_id in self.data_manager.products:
            product = self.data_manager.products[product_id]
            auto_content = product.get('auto_delivery_content', '')
            
            if auto_content:
                # 使用管理员设置的自动发货内容
                delivery_content = auto_content
            else:
                # 如果没有设置自动发货内容，生成默认内容
                card_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
                delivery_content = f"您的商品卡密：{card_code}\n请妥善保管，勿泄露给他人"
        else:
            # 商品不存在，生成默认内容
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
        
        # 更新订单状态为已发货
        order_data['status'] = 'delivered'
        order_data['delivered_at'] = datetime.now()
        self.data_manager.save_orders()
        
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
            f"💳 支付方式：{order_data.get('payment_method', '未知')}\n"
            f"⏰ 时间：{order_data.get('paid_at', '未知')}\n"
            f"请使用 /deliver_order {order_no} 发货内容 进行处理"
        )
        
        # 发送消息给管理员
        await self._send_message_to_admin(admin_message)

    async def _send_message_to_admin(self, message: str):
        """发送消息给管理员"""
        try:
            # 这里需要根据实际情况获取管理员的会话标识
            # 示例：从配置中读取管理员ID
            admin_ids = self.config.get('admin_ids', [])
            
            for admin_id in admin_ids:
                try:
                    # 使用AstrBot的API发送消息给管理员
                    await self.context.send_message(
                        admin_id, 
                        [Comp.Plain(text=message)]
                    )
                except Exception as e:
                    logger.error(f"发送消息给管理员 {admin_id} 失败: {e}")
        except Exception as e:
            logger.error(f"发送管理员通知失败: {e}")

    # 简化的购物车功能
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

    # 简化的购物车购买流程
    @filter.command("cart_buy")
    async def buy_cart(self, event: AstrMessageEvent):
        """购买购物车所有商品（简化版，直接使用第一个支付方式）"""
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
        
        # 获取第一个可用的支付方式
        available_methods = []
        for method_id, method in self.data_manager.payment_methods.items():
            if method.get('enabled', True):
                available_methods.append((method_id, method['name']))
        
        if not available_methods:
            yield event.plain_result("暂无可用支付方式，请联系管理员")
            return
        
        # 使用第一个支付方式
        method_id, method_name = available_methods[0]
        
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
            payment_method=method_name,
            expire_time=expire_time,
            created_at=datetime.now(),
            cart_items=[
                {
                    'product_id': item['product_id'],
                    'name': item['name'],
                    'price': item['price'],
                    'quantity': item['quantity'],
                    'delivery_type': item['delivery_type']
                }
                for item in self.data_manager.carts[user_id]
            ]
        )
        
        # 生成支付信息
        payment_result = await self.payment_service.create_payment(
            order_no=order_no,
            amount=total_amount,
            product_name="购物车商品",
            payment_method=method_id
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
            f"💳 支付方式：{method_name}\n"
            f"⏰ 请在{self.payment_timeout}秒内完成支付\n"
            f"📋 订单号：{order_no}"
        )
        
        # 发送支付二维码
        yield event.image_result(qr_buffer)
        
        # 发送支付链接
        yield event.plain_result(f"支付链接：{payment_result['payment_url']}")

    # 订单管理功能
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
            payment_method = order_data.get('payment_method', '未知')
            
            result = (
                f"📋 订单详情：\n"
                f"订单号：{order_no}\n"
                f"状态：{status_text}\n"
                f"商品：{order_data['product_name']}\n"
                f"数量：{order_data['quantity']}\n"
                f"金额：¥{order_data['amount']}\n"
                f"支付方式：{payment_method}"
            )
            
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
            
            # 修复排序逻辑：将字符串转换为datetime对象进行比较
            user_orders.sort(
                key=lambda x: (
                    x[1].get('created_at') 
                    if isinstance(x[1].get('created_at'), datetime)
                    else datetime.fromisoformat(x[1]['created_at']) 
                    if x[1].get('created_at') 
                    else datetime.min
                ), 
                reverse=True
            )
            
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("order_list")
    async def list_orders(self, event: AstrMessageEvent, status: str = "all", page: int = 1):
        """管理员查看订单列表"""
        page_size = 10
        filtered_orders = []
        
        for order_no, order_data in self.data_manager.orders.items():
            if status == "all" or order_data.get('status') == status:
                filtered_orders.append((order_no, order_data))
        
        # 按创建时间倒序排列（修复排序逻辑）
        filtered_orders.sort(
            key=lambda x: (
                x[1].get('created_at') 
                if isinstance(x[1].get('created_at'), datetime)
                else datetime.fromisoformat(x[1]['created_at']) 
                if x[1].get('created_at') 
                else datetime.min
            ), 
            reverse=True
        )
        
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
            order_list += f"   时间：{order_data.get('created_at', '').strftime('%Y-%m-%d %H:%M:%S') if isinstance(order_data.get('created_at'), datetime) else order_data.get('created_at', '')[:19]}\n\n"
        
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
        order_data['cancelled_at'] = datetime.now()
        order_data['cancelled_by'] = 'user' if order_data['user_id'] == user_id else 'admin'
        
        self.data_manager.save_orders()
        
        # 如果订单有支付监控任务，取消它
        if order_no in self.data_manager.payment_monitors:
            self.data_manager.payment_monitors[order_no].cancel()
            del self.data_manager.payment_monitors[order_no]
        
        yield event.plain_result(f"✅ 订单 {order_no} 已取消")

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
        order_data['delivered_at'] = datetime.now()
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
        
        # 支付方式统计
        payment_stats = {}
        for order in self.data_manager.orders.values():
            method = order.get('payment_method', '未知')
            payment_stats[method] = payment_stats.get(method, 0) + 1
        
        stats = f"📊 商城统计\n\n"
        stats += f"📦 商品数量：{total_products}\n"
        stats += f"📋 订单总数：{total_orders}\n"
        stats += f"💰 总营业额：¥{revenue:.2f}\n"
        stats += f"👥 注册用户：{total_users}\n\n"
        
        stats += "💳 支付方式统计：\n"
        for method, count in payment_stats.items():
            stats += f"  {method}: {count} 单\n"
        
        yield event.plain_result(stats)

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
                data_files = [
                    "products.json",
                    "orders.json", 
                    "user_emails.json",
                    "payment_methods.json"
                ]
                
                for file in data_files:
                    file_path = os.path.join(self.data_dir, file)
                    if os.path.exists(file_path):
                        zipf.write(file_path, file)
            
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

    # 插件版本检查
    @filter.command("mall_version")
    async def mall_version(self, event: AstrMessageEvent):
        """查看插件版本"""
        yield event.plain_result(f"🛍️ 商城插件版本: v{self.plugin_version}")

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
        status_report += f"⏰ 支付超时：{self.payment_timeout}秒\n"
        status_report += f"🔒 库存锁数量：{len(self.product_locks)}"
        
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
            self.data_manager.save_payment_methods()
            
            logger.info("商城插件数据已保存")
        except Exception as e:
            logger.error(f"插件终止时发生错误: {e}")

    # 支付回调处理（Webhook端点）
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
/product_info <商品ID> - 查看商品详情
/buy <商品ID> [数量] - 购买商品（可选择支付方式）
/cart_add <商品ID> [数量] - 加入购物车
/cart - 查看购物车
/cart_buy - 购买购物车所有商品
/cart_remove <序号> - 移除购物车商品
/cart_clear - 清空购物车
/check_order [订单号] - 查看订单
/cancel_order <订单号> - 取消订单
/mall_status - 查看系统状态
/mall_version - 查看插件版本

👑 管理员命令：
/add_product <名称> <价格> <库存> [发货方式] [描述] [自动发货内容] - 添加商品
/set_auto_delivery <商品ID> <内容> - 设置自动发货内容
/add_payment_method <ID> <名称> <类型> [启用] - 添加支付方式
/list_payment_methods - 查看支付方式列表
/toggle_payment_method <ID> <启用状态> - 启用/禁用支付方式
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
5. 购买时可选择不同的支付方式
6. 支持购物车批量购买
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

# 邮箱绑定功能（保持原有功能）
@filter.command("bind_email")
async def bind_email(self, event: AstrMessageEvent, email: str):
    """绑定邮箱"""
    user_id = event.get_sender_id()
    
    # 首先检查邮箱服务是否配置
    if not self.email_service.enabled:
        yield event.plain_result("❌ 邮箱服务未配置，请联系管理员配置邮箱服务")
        return
    
    # 检查邮箱配置是否完整
    email_config = self.config.get('email_config', {})
    if not all([email_config.get('smtp_host'), 
               email_config.get('smtp_username'), 
               email_config.get('smtp_password')]):
        yield event.plain_result("❌ 邮箱配置不完整，请联系管理员检查配置")
        return
    
    # 生成验证码
    verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    
    # 保存验证码到临时状态字典
    user_id = event.get_sender_id()
    self.temp_orders[f"verify_{user_id}"] = {
        'code': verification_code,
        'email': email,
        'expire_time': datetime.now() + timedelta(minutes=10)
    }
    
    # 发送验证邮件
    logger.info(f"尝试向 {email} 发送验证邮件")
    success = await self.email_service.send_verification_code(email, verification_code)
    
    if success:
        yield event.plain_result(f"✅ 验证码已发送到 {email}，请使用 /verify_email 验证码 完成绑定")
    else:
        # 清理临时数据
        if f"verify_{user_id}" in self.temp_orders:
            del self.temp_orders[f"verify_{user_id}"]
        yield event.plain_result(
            f"❌ 邮件发送失败\n"
            f"可能的原因：\n"
            f"1. 邮箱地址格式错误\n"
            f"2. SMTP服务器配置错误\n"
            f"3. 邮箱账号或密码错误\n"
            f"4. 网络连接问题\n"
            f"请检查邮箱配置或联系管理员"
        )

@filter.command("verify_email")
async def verify_email(self, event: AstrMessageEvent, code: str):
    """验证邮箱"""
    user_id = event.get_sender_id()
    verification_key = f"verify_{user_id}"
    
    verification_data = self.temp_orders.get(verification_key)
    if not verification_data or verification_data['expire_time'] < datetime.now():
        if verification_key in self.temp_orders:
            del self.temp_orders[verification_key]
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
        if verification_key in self.temp_orders:
            del self.temp_orders[verification_key]
        
        yield event.plain_result("✅ 邮箱绑定成功！")
    else:
        yield event.plain_result("❌ 验证码错误，请重新输入")
