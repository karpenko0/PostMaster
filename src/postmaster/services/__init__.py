"""Прикладные сервисы: бизнес-логика PostMaster.

Сервисы не импортируют telebot и handlers (BR-01, BR-04). Данные они получают
через repositories, публикуют через publishers. Scheduler вызывает сервисы.
"""
