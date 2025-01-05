import asyncio
import logging
from eth_abi import abi
from client import Client
from config import TOKENS_PER_CHAIN, STARGATE_CONTRACTS, STARGATE_ETH_ABI, CHAIN_ID_BY_NAME, STARGATE_USDC_ABI
from functions import get_network, get_rpc_explorer, get_token, get_amount
from termcolor import colored
from web3 import AsyncWeb3, AsyncHTTPProvider

# Настройка логирования
file_log = logging.FileHandler('stargate.log', encoding='utf-8')
console_out = logging.StreamHandler()
logging.basicConfig(handlers=(file_log, console_out),
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

class CustomError(Exception):
    """Базовый класс для других исключений"""
    pass

class ContractNotFound(CustomError):
    """Вызывается, когда контракт не найден"""

    def __init__(self, message="Контракт не найден!"):
        self.message = message
        super().__init__(self.message)

class InvalidPrivateKey(CustomError):
    """Вызывается, когда приватный ключ недействителен"""

    def __init__(self, message="Недействительный приватный ключ!"):
        self.message = message
        super().__init__(self.message)

class TransactionError(CustomError):
    """Вызывается, когда происходит ошибка в транзакции"""

    def __init__(self, message="Ошибка транзакции!"):
        self.message = message
        super().__init__(self.message)

class Stargate(Client):
    """
    Класс для взаимодействия с контрактом Stargate bridge.

    Атрибуты:
        private_key (str): Приватный ключ для Ethereum аккаунта.
        proxy (str): Прокси для HTTP запросов.
        chain_name_dict (dict): Словарь доступных сетей.
        chain_name (str): Имя выбранной сети.
        chain_id (int): ID выбранной сети.
        chain_token (str): Токен для бриджа.
        proxy (str): Прокси для HTTP запросов.
        eip_1559 (bool): Использовать ли EIP-1559 транзакции.
        explorer_url (str): URL блокчейн эксплорера.
        rpc_url (str): URL RPC эндпоинта.
        w3 (AsyncWeb3): Экземпляр Web3.
        address (str): Ethereum адрес, полученный из приватного ключа.
    """

    def __init__(self, private_key, proxy):
        """
        Инициализирует экземпляр Stargate.

        Аргументы:
            private_key (str): Приватный ключ для Ethereum аккаунта.
            proxy (str): Прокси для HTTP запросов.
        """
        self.private_key = private_key
        request_kwargs = {'proxy': f'http://{proxy}'}
        self.chain_name_dict = get_network('Выберите сеть из которой будет сделан бридж токена')
        self.chain_name, self.chain_id = list(self.chain_name_dict.items())[0]
        self.chain_token = get_token()
        self.chain_id = CHAIN_ID_BY_NAME[self.chain_name]
        self.proxy = proxy
        self.eip_1559 = True
        self.explorer_url = get_rpc_explorer(self.chain_name)['explorers'][0]
        self.rpc_url = get_rpc_explorer(self.chain_name)['rpc'][0]
        try:
            self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url, request_kwargs=request_kwargs))
            self.address = self.w3.to_checksum_address(self.w3.eth.account.from_key(self.private_key).address)
        except Exception as er:
            logging.error(f"{er}")
            raise InvalidPrivateKey(f"Недействительный приватный ключ! {er}")

    async def get_bridge_fee(self, send_params):
        """
        Получает комиссию за бридж для заданных параметров отправки.

        Аргументы:
            send_params (list): Параметры для транзакции бриджа.

        Возвращает:
            int: Комиссия за бридж.
        """
        bridge_fee = await self.bridge_contract.functions.quoteSend(
            send_params,
            False
        ).call()
        return bridge_fee

    async def bridge(self, token_address: str, amount_in_wei: int, to_chain_id: int):
        """
        Осуществляет бридж указанного количества токенов на целевую сеть.

        Аргументы:
            token_address (str): Адрес токена для бриджа.
            amount_in_wei (int): Количество токенов для бриджа в wei.
            to_chain_id (int): ID целевой сети.

        Возвращает:
            str: Хэш транзакции.
        """
        # Определение контракта и ABI в зависимости от типа токена
        if self.chain_token == "ETH":
            contract_address = STARGATE_CONTRACTS[self.chain_name]['pool_eth']
            abi_token = STARGATE_ETH_ABI
        elif self.chain_token == "USDC":
            contract_address = STARGATE_CONTRACTS[self.chain_name]['pool_usdc']
            logging.info("Делаем апрув")
            await self.make_approve(token_address, contract_address, amount_in_wei)
            abi_token = STARGATE_USDC_ABI

        try:
            # Получение контракта
            self.bridge_contract = self.get_contract(contract_address=contract_address, abi=abi_token)
        except ContractNotFound as er:
            logging.error(er)

        # Подготовка параметров для отправки
        send_params = [
            to_chain_id,
            abi.encode(['address'], [self.address]),
            amount_in_wei,
            int(amount_in_wei * 0.995),
            '0x',
            '0x',
            '0x'
        ]

        # Получение комиссии за бридж
        bridge_fee = await self.get_bridge_fee(send_params)
        if self.chain_token == "ETH":
            value = int(bridge_fee[0] + amount_in_wei)
        else:
            value = bridge_fee[0]

        # Подготовка транзакции
        transaction = await self.bridge_contract.functions.send(
            send_params,
            bridge_fee,
            self.address
        ).build_transaction(await self.prepare_tx(value))

        # Отправка транзакции
        try:
            logging.info("Делаем транзакцию")
            tx_hash = await self.send_transaction(transaction)
            logging.info(f"Транзакция отправлена: {tx_hash}")
            return tx_hash
        except Exception as e:
            logging.error(f"Ошибка транзакции: {e}")
            raise TransactionError(f"Ошибка транзакции: {e}")

async def main():
    """
    Основная функция для взаимодействия с пользователем и контрактом Stargate.
    """
    proxy = ''
    # Получение private_key от пользователя
    private_key = input(colored("Введите свой private key: ", 'light_green'))
    stargate = Stargate(private_key=private_key, proxy=proxy)
    token_address = TOKENS_PER_CHAIN[stargate.chain_name][stargate.chain_token]
    balance = await stargate.get_balance(token_address)
    amount_in_wei = get_amount(balance)
    to_chain = get_network("Выберите сеть куда будет сделан бридж: ")
    _, to_chain_id = list(to_chain.items())[0]
    await stargate.bridge(token_address, amount_in_wei, to_chain_id)

# Запуск основной функции
asyncio.run(main())
