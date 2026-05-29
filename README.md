battle-bot/
│
├── .gitignore
├── README.md
├── requirements.txt
│
├── src/
│   ├── __init__.py
│   └── broker/
│       ├── __init__.py
│       └── ibkr/
│           ├── __init__.py
│           └── trading_client.py   <-- Relocated here
│
└── tests/
    ├── __init__.py
    └── broker/
        └── ibkr/
            ├── __init__.py
            ├── test_trading_client.py  <-- Relocated here
            └── integration_test.py     <-- Relocated here