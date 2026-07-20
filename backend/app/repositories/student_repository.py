from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Student


class StudentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_or_create(self, name: str, student_number: str = "") -> Student:
        stmt = select(Student).where(Student.name == name, Student.student_number == student_number)
        student = self.db.execute(stmt).scalars().first()
        if student is None:
            student = Student(name=name, student_number=student_number)
            self.db.add(student)
            self.db.flush()
        return student

    def get(self, student_id: str) -> Student | None:
        return self.db.get(Student, student_id)
